from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import quote

import httpx

from .external_trigger_v2 import (
    CONTROL_PATH,
    BootstrapTrigger,
    ExternalControlError,
    ExternalControlErrorReason,
    ExternalDispatchPlan,
    dispatch_plan_for,
    validate_control_scope,
    validate_external_trigger,
    validate_trigger_actor,
)


API_VERSION = "2022-11-28"
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class ControlChangeSet:
    before: str
    after: str
    changed_paths: tuple[str, ...]
    commits: int


class ExternalRelayAdapter(Protocol):
    def read_change_set(self, before: str, after: str) -> ControlChangeSet: ...

    def read_control_payload(self, commit_sha: str) -> bytes: ...

    def dispatch(self, plan: ExternalDispatchPlan) -> None: ...


class GitHubExternalRelayAdapter:
    """Read one immutable control change and dispatch one fixed writer."""

    def __init__(self, client: httpx.Client, *, repository: str) -> None:
        if not _REPOSITORY.fullmatch(repository):
            raise ValueError("repository must use owner/name syntax")
        self._client = client
        self._repository = repository

    def read_change_set(self, before: str, after: str) -> ControlChangeSet:
        before_sha = _sha(before, "before")
        after_sha = _sha(after, "after")
        payload = self._request_json(
            "GET",
            f"/repos/{self._repository}/compare/{before_sha}...{after_sha}",
            expected_status=200,
        )
        if payload.get("status") != "ahead":
            _io_error("control comparison status must be ahead")
        ahead_by = payload.get("ahead_by")
        total_commits = payload.get("total_commits")
        if ahead_by != 1 or total_commits != 1:
            _scope_error("control push must advance exactly one commit")
        base = _object(payload.get("base_commit"), "compare.base_commit")
        if _sha(base.get("sha"), "compare.base_commit.sha") != before_sha:
            _io_error("compare base SHA does not match push before")
        commits_payload = payload.get("commits")
        if not isinstance(commits_payload, list) or len(commits_payload) != 1:
            _io_error("compare.commits must contain the single pushed commit")
        head = _object(commits_payload[0], "compare.commits[0]")
        if _sha(head.get("sha"), "compare.commits[0].sha") != after_sha:
            _io_error("compare head SHA does not match push after")
        files = payload.get("files")
        if not isinstance(files, list):
            _io_error("compare.files must be a list")
        paths: list[str] = []
        for raw_file in files:
            file = _object(raw_file, "compare file")
            filename = file.get("filename")
            if not isinstance(filename, str) or not filename:
                _io_error("compare file has no filename")
            paths.append(filename)
        if len(set(paths)) != len(paths):
            _io_error("compare response contains duplicate paths")
        return ControlChangeSet(
            before_sha,
            after_sha,
            tuple(sorted(paths)),
            total_commits,
        )

    def read_control_payload(self, commit_sha: str) -> bytes:
        sha = _sha(commit_sha, "commit_sha")
        encoded_path = "/".join(quote(part, safe="") for part in CONTROL_PATH.split("/"))
        payload = self._request_json(
            "GET",
            f"/repos/{self._repository}/contents/{encoded_path}",
            params={"ref": sha},
            expected_status=200,
        )
        if payload.get("type") != "file" or payload.get("encoding") != "base64":
            _io_error("control payload must be a base64 file")
        blob_sha = _sha(payload.get("sha"), "contents.sha")
        encoded = payload.get("content")
        if not isinstance(encoded, str):
            _io_error("control payload content must be base64")
        try:
            content = base64.b64decode("".join(encoded.split()), validate=True)
        except ValueError as exc:
            raise ExternalControlError(
                ExternalControlErrorReason.INVALID_EXTERNAL_TRIGGER,
                "control payload is not valid base64",
            ) from exc
        size = payload.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size != len(content):
            _io_error("control payload size does not match decoded content")
        if _git_blob_sha(content) != blob_sha:
            _io_error("control payload bytes do not match blob SHA")
        return content

    def dispatch(self, plan: ExternalDispatchPlan) -> None:
        response = self._request(
            "POST",
            f"/repos/{self._repository}/actions/workflows/{quote(plan.workflow, safe='')}/dispatches",
            json_body={"ref": plan.ref, "inputs": plan.inputs},
        )
        if response.status_code != 204:
            _io_error(
                f"workflow_dispatch returned HTTP {response.status_code}",
                status_code=response.status_code,
            )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        expected_status: int,
    ) -> dict[str, Any]:
        response = self._request(method, path, params=params)
        if response.status_code != expected_status:
            _io_error(
                f"GitHub read returned HTTP {response.status_code}",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ExternalControlError(
                ExternalControlErrorReason.EXTERNAL_DISPATCH_FAILED,
                "GitHub response is not valid JSON",
            ) from exc
        return _object(payload, "GitHub response")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Any | None = None,
    ) -> httpx.Response:
        try:
            return self._client.request(
                method,
                path,
                params=params,
                json=json_body,
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION,
                },
            )
        except httpx.RequestError as exc:
            raise ExternalControlError(
                ExternalControlErrorReason.EXTERNAL_DISPATCH_FAILED,
                f"GitHub request failed: {exc}",
            ) from exc


def run_external_relay(
    adapter: ExternalRelayAdapter,
    *,
    ref: str,
    before: str,
    after: str,
    actor: str,
    actor_id: str | int | None,
    now: datetime,
    summary_path: Path | None = None,
) -> dict[str, Any]:
    canonical_actor, canonical_actor_id = validate_trigger_actor(actor, actor_id)
    changes = adapter.read_change_set(before, after)
    validate_control_scope(
        ref=ref,
        before=changes.before,
        after=changes.after,
        changed_paths=changes.changed_paths,
        commits=changes.commits,
    )
    raw = adapter.read_control_payload(changes.after)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExternalControlError(
            ExternalControlErrorReason.INVALID_EXTERNAL_TRIGGER,
            "control payload must be UTF-8 JSON",
        ) from exc
    trigger = validate_external_trigger(payload, now=now)

    if isinstance(trigger, BootstrapTrigger):
        result = {
            "trigger_result": "IGNORED_BOOTSTRAP",
            "actor": canonical_actor,
            "actor_id": canonical_actor_id,
            "before": changes.before,
            "after": changes.after,
            "changed_paths": list(changes.changed_paths),
            "dispatch": {"attempted": "NO", "accepted": "NO"},
        }
    else:
        plan = dispatch_plan_for(trigger)
        adapter.dispatch(plan)
        result = {
            "trigger_result": "RELAY_ACCEPTED",
            "actor": canonical_actor,
            "actor_id": canonical_actor_id,
            "before": changes.before,
            "after": changes.after,
            "changed_paths": list(changes.changed_paths),
            "target_report_date": trigger.target_report_date.isoformat(),
            "mode": trigger.mode.value,
            "request_id": trigger.request_id,
            "requested_at": trigger.requested_at.isoformat(),
            "source": trigger.source,
            "dispatch": {
                "attempted": "YES",
                "accepted": "YES",
                "workflow": plan.workflow,
                "ref": plan.ref,
                "inputs": plan.inputs,
            },
        }
    if summary_path is not None:
        _append_summary(summary_path, result)
    return result


def _append_summary(path: Path, result: Mapping[str, Any]) -> None:
    dispatch = result["dispatch"]
    lines = [
        "## AI HOT V2 External Recovery Relay",
        "",
        f"- TRIGGER_RESULT: `{result['trigger_result']}`",
        f"- actor: `{result['actor']}`",
        f"- actor_id: `{result['actor_id']}`",
        f"- before: `{result['before']}`",
        f"- after: `{result['after']}`",
        f"- changed_paths: `{', '.join(result['changed_paths'])}`",
    ]
    if result["trigger_result"] == "RELAY_ACCEPTED":
        lines.extend(
            [
                f"- target_report_date: `{result['target_report_date']}`",
                f"- mode: `{result['mode']}`",
                f"- request_id: `{result['request_id']}`",
                f"- requested_at: `{result['requested_at']}`",
                f"- source: `{result['source']}`",
                f"- fixed workflow: `{dispatch['workflow']}`",
                f"- fixed ref: `{dispatch['ref']}`",
            ]
        )
    lines.extend(
        [
            f"- DISPATCH_ATTEMPTED: `{dispatch['attempted']}`",
            f"- DISPATCH_ACCEPTED: `{dispatch['accepted']}`",
            "- RELAY_REPOSITORY_WRITE: `NO`",
            "- PRODUCER_EXECUTED_BY_RELAY: `NO`",
            "",
        ]
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def _append_failure_summary(path: Path, error: Exception) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            "## AI HOT V2 External Recovery Relay\n\n"
            "- TRIGGER_RESULT: `FAIL_CLOSED`\n"
            f"- error: `{str(error).replace('`', '')}`\n"
            "- RELAY_REPOSITORY_WRITE: `NO`\n"
            "- PRODUCER_EXECUTED_BY_RELAY: `NO`\n"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Relay one external V2 recovery trigger")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--actor-id")
    parser.add_argument("--summary", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("external relay failed: GITHUB_TOKEN is required", file=sys.stderr)
        return 1
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "aihot-data-bridge-v2-external-relay",
    }
    try:
        with httpx.Client(
            base_url="https://api.github.com",
            headers=headers,
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=False,
        ) as client:
            result = run_external_relay(
                GitHubExternalRelayAdapter(client, repository=args.repository),
                ref=args.ref,
                before=args.before,
                after=args.after,
                actor=args.actor,
                actor_id=args.actor_id,
                now=datetime.now(timezone.utc),
                summary_path=args.summary,
            )
    except Exception as exc:
        if args.summary is not None:
            _append_failure_summary(args.summary, exc)
        print(f"external relay failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        _io_error(f"{name} must be a full lowercase Git SHA")
    return value


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _io_error(f"{name} must be an object")
    return value


def _git_blob_sha(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


def _scope_error(detail: str) -> None:
    raise ExternalControlError(
        ExternalControlErrorReason.CONTROL_SCOPE_VIOLATION,
        detail,
    )


def _io_error(detail: str, *, status_code: int | None = None) -> None:
    raise ExternalControlError(
        ExternalControlErrorReason.EXTERNAL_DISPATCH_FAILED,
        detail,
        status_code=status_code,
    )


if __name__ == "__main__":
    raise SystemExit(main())

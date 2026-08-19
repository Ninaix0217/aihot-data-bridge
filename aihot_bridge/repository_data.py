from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time as time_module
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .snapshot import (
    SnapshotRejected,
    ensure_trustworthy_snapshot,
    load_snapshot,
    validate_snapshot,
)


DATA_BRANCH = "snapshot-data"
BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
API_ROOT = "https://api.github.com"


class RepositoryDataError(RuntimeError):
    """The repository candidate cannot be published or trusted."""


@dataclass(frozen=True)
class CandidateMetadata:
    report_date: str
    path: str
    generated_at: str
    sha256: str
    window_from: str
    window_to: str
    raw_items: int
    deduplicated_items: int


@dataclass(frozen=True)
class RepositoryCandidate:
    metadata: CandidateMetadata
    content: bytes
    commit_sha: str
    blob_sha: str


def report_window(report_day: date) -> tuple[datetime, datetime]:
    report_end = datetime.combine(
        report_day,
        time(hour=12),
        tzinfo=BEIJING_TIMEZONE,
    )
    return report_end - timedelta(days=1), report_end


def report_date_for_payload(payload: dict[str, Any]) -> date:
    generated_at = _parse_timestamp(payload.get("generated_at"), "generated_at")
    return generated_at.astimezone(BEIJING_TIMEZONE).date()


def validate_candidate_for_report(
    payload: dict[str, Any],
    report_day: date,
    *,
    now: datetime | None = None,
) -> CandidateMetadata:
    try:
        validate_snapshot(payload)
        ensure_trustworthy_snapshot(payload)
    except SnapshotRejected as exc:
        raise RepositoryDataError(str(exc)) from exc

    if report_date_for_payload(payload) != report_day:
        raise RepositoryDataError(
            "candidate generated_at does not match the Beijing report date"
        )

    window = payload["window"]
    if window.get("type") != "rolling_candidate" or window.get("hours") != 30:
        raise RepositoryDataError("candidate must use the rolling 30h window")

    candidate_start = _parse_timestamp(window.get("from"), "window.from")
    candidate_end = _parse_timestamp(window.get("to"), "window.to")
    report_start, report_end = report_window(report_day)
    report_start_utc = report_start.astimezone(timezone.utc)
    report_end_utc = report_end.astimezone(timezone.utc)
    if candidate_start > report_start_utc or candidate_end < report_end_utc:
        raise RepositoryDataError(
            "candidate does not cover the fixed report window: "
            f"candidate=[{candidate_start.isoformat()}, {candidate_end.isoformat()}) "
            f"report=[{report_start_utc.isoformat()}, {report_end_utc.isoformat()})"
        )

    generated_at = _parse_timestamp(payload["generated_at"], "generated_at")
    if generated_at < report_end_utc:
        raise RepositoryDataError(
            "candidate was generated before the report window ended"
        )
    if now is not None:
        if now.tzinfo is None:
            raise ValueError("now must include a timezone")
        if generated_at > now.astimezone(timezone.utc) + timedelta(minutes=5):
            raise RepositoryDataError(
                "candidate generated_at is implausibly in the future"
            )

    summary = payload["summary"]
    return CandidateMetadata(
        report_date=report_day.isoformat(),
        path=f"report-candidate/{report_day.isoformat()}.json",
        generated_at=payload["generated_at"],
        sha256="",
        window_from=window["from"],
        window_to=window["to"],
        raw_items=summary["raw_items"],
        deduplicated_items=summary["deduplicated_items"],
    )


def stage_candidate(candidate_path: Path, data_root: Path) -> CandidateMetadata:
    try:
        content = candidate_path.read_bytes()
        payload = load_snapshot(candidate_path)
    except (OSError, SnapshotRejected) as exc:
        raise RepositoryDataError(f"cannot stage candidate: {exc}") from exc

    report_day = report_date_for_payload(payload)
    metadata = validate_candidate_for_report(payload, report_day)
    metadata = _metadata_with_sha256(metadata, content)

    dated_path = data_root / metadata.path
    latest_path = data_root / "latest.json"
    dated_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    dated_path.write_bytes(content)
    latest_path.write_bytes(content)
    return metadata


def read_repository_candidate(
    *,
    repository: str,
    branch: str,
    path: str,
    token: str,
    expected_commit_sha: str | None = None,
    opener: Callable[..., Any] | None = None,
    timeout_seconds: float = 20,
) -> RepositoryCandidate:
    encoded_branch = urllib.parse.quote(branch, safe="")
    ref_payload = _get_github_json(
        f"{API_ROOT}/repos/{repository}/git/ref/heads/{encoded_branch}",
        token=token,
        opener=opener,
        timeout_seconds=timeout_seconds,
    )
    commit_sha = _nested_string(ref_payload, "object", "sha")
    if expected_commit_sha is not None and commit_sha != expected_commit_sha:
        raise RepositoryDataError(
            "snapshot-data branch has not reached the expected commit: "
            f"public={commit_sha} expected={expected_commit_sha}"
        )

    encoded_path = urllib.parse.quote(path, safe="/")
    query = urllib.parse.urlencode({"ref": commit_sha})
    content_payload = _get_github_json(
        f"{API_ROOT}/repos/{repository}/contents/{encoded_path}?{query}",
        token=token,
        opener=opener,
        timeout_seconds=timeout_seconds,
    )
    if content_payload.get("encoding") != "base64":
        raise RepositoryDataError("GitHub Contents API did not return base64 content")
    encoded_content = content_payload.get("content")
    if not isinstance(encoded_content, str):
        raise RepositoryDataError("GitHub Contents API response is missing content")
    try:
        content = base64.b64decode(encoded_content, validate=False)
    except (ValueError, TypeError) as exc:
        raise RepositoryDataError("GitHub content is not valid base64") from exc

    blob_sha = content_payload.get("sha")
    if not isinstance(blob_sha, str) or not blob_sha:
        raise RepositoryDataError("GitHub Contents API response is missing blob SHA")
    calculated_blob_sha = _git_blob_sha(content)
    if blob_sha != calculated_blob_sha:
        raise RepositoryDataError(
            f"GitHub blob SHA mismatch: api={blob_sha} calculated={calculated_blob_sha}"
        )

    payload = _decode_candidate(content)
    report_day = _report_day_from_path(path)
    metadata = _metadata_with_sha256(
        validate_candidate_for_report(payload, report_day),
        content,
    )
    return RepositoryCandidate(
        metadata=metadata,
        content=content,
        commit_sha=commit_sha,
        blob_sha=blob_sha,
    )


def verify_repository_candidate(
    *,
    repository: str,
    branch: str,
    path: str,
    token: str,
    expected_content: bytes,
    expected_generated_at: str,
    expected_commit_sha: str,
    opener: Callable[..., Any] | None = None,
    timeout_seconds: float = 20,
) -> RepositoryCandidate:
    candidate = read_repository_candidate(
        repository=repository,
        branch=branch,
        path=path,
        token=token,
        expected_commit_sha=expected_commit_sha,
        opener=opener,
        timeout_seconds=timeout_seconds,
    )
    expected_sha256 = hashlib.sha256(expected_content).hexdigest()
    if candidate.content != expected_content:
        raise RepositoryDataError(
            "repository candidate does not match the build artifact: "
            f"repository={candidate.metadata.sha256} expected={expected_sha256}"
        )
    if candidate.metadata.generated_at != expected_generated_at:
        raise RepositoryDataError(
            "repository candidate generated_at does not match the build artifact"
        )
    return candidate


def verify_repository_candidate_with_retries(
    *,
    attempts: int,
    interval_seconds: float,
    **kwargs: Any,
) -> RepositoryCandidate:
    if attempts < 1:
        raise ValueError("attempts must be at least one")
    if interval_seconds < 0:
        raise ValueError("interval_seconds cannot be negative")
    last_error: RepositoryDataError | None = None
    for attempt in range(1, attempts + 1):
        try:
            candidate = verify_repository_candidate(**kwargs)
            print(f"repository read-back attempt {attempt}/{attempts}: VERIFIED")
            return candidate
        except RepositoryDataError as exc:
            last_error = exc
            print(
                f"repository read-back attempt {attempt}/{attempts}: {exc}",
                file=sys.stderr,
            )
            if attempt < attempts:
                time_module.sleep(interval_seconds)
    assert last_error is not None
    raise last_error


def _get_github_json(
    url: str,
    *,
    token: str,
    opener: Callable[..., Any] | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "aihot-data-bridge/0.2",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout_seconds) as response:
            status = getattr(response, "status", None)
            if status != 200:
                raise RepositoryDataError(f"GitHub API returned HTTP {status}")
            body = response.read()
    except RepositoryDataError:
        raise
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
    ) as exc:
        raise RepositoryDataError(f"GitHub API request failed: {exc}") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepositoryDataError("GitHub API returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise RepositoryDataError("GitHub API JSON root must be an object")
    return payload


def _decode_candidate(content: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepositoryDataError("repository candidate is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RepositoryDataError("repository candidate JSON root must be an object")
    return payload


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RepositoryDataError(f"{field} is missing or is not a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RepositoryDataError(f"{field} is not valid ISO 8601") from exc
    if parsed.tzinfo is None:
        raise RepositoryDataError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _report_day_from_path(path: str) -> date:
    prefix = "report-candidate/"
    if not path.startswith(prefix) or not path.endswith(".json"):
        raise RepositoryDataError("repository path is not a dated candidate path")
    try:
        return date.fromisoformat(path[len(prefix) : -len(".json")])
    except ValueError as exc:
        raise RepositoryDataError(
            "repository candidate path has an invalid date"
        ) from exc


def _metadata_with_sha256(
    metadata: CandidateMetadata,
    content: bytes,
) -> CandidateMetadata:
    return CandidateMetadata(
        report_date=metadata.report_date,
        path=metadata.path,
        generated_at=metadata.generated_at,
        sha256=hashlib.sha256(content).hexdigest(),
        window_from=metadata.window_from,
        window_to=metadata.window_to,
        raw_items=metadata.raw_items,
        deduplicated_items=metadata.deduplicated_items,
    )


def _git_blob_sha(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


def _nested_string(payload: dict[str, Any], *keys: str) -> str:
    value: Any = payload
    for key in keys:
        if not isinstance(value, dict):
            raise RepositoryDataError("GitHub API response has an unexpected shape")
        value = value.get(key)
    if not isinstance(value, str) or not value:
        raise RepositoryDataError("GitHub API response is missing a required value")
    return value


def _metadata_as_dict(
    metadata: CandidateMetadata,
    *,
    commit_sha: str | None = None,
    blob_sha: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "report_date": metadata.report_date,
        "path": metadata.path,
        "generated_at": metadata.generated_at,
        "sha256": metadata.sha256,
        "candidate_window": [metadata.window_from, metadata.window_to],
        "raw_items": metadata.raw_items,
        "deduplicated_items": metadata.deduplicated_items,
    }
    if commit_sha is not None:
        result["commit_sha"] = commit_sha
    if blob_sha is not None:
        result["blob_sha"] = blob_sha
    return result


def _write_summary(
    *,
    title: str,
    metadata: CandidateMetadata,
    repository: str,
    branch: str,
    commit_sha: str | None = None,
    blob_sha: str | None = None,
) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        f"## {title}",
        "",
        f"- Repository: `{repository}`",
        f"- Branch: `{branch}`",
        f"- Path: `{metadata.path}`",
        f"- generated_at: `{metadata.generated_at}`",
        f"- SHA-256: `{metadata.sha256}`",
        f"- Candidate window: `[{metadata.window_from}, {metadata.window_to})`",
        f"- raw_items: `{metadata.raw_items}`",
        f"- deduplicated_items: `{metadata.deduplicated_items}`",
    ]
    if commit_sha is not None:
        lines.append(f"- Commit SHA: `{commit_sha}`")
    if blob_sha is not None:
        lines.append(f"- Blob SHA: `{blob_sha}`")
    lines.append("- Result: `VERIFIED`")
    with Path(summary_path).open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _write_outputs(candidate: RepositoryCandidate) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as handle:
        handle.write(f"commit_sha={candidate.commit_sha}\n")
        handle.write(f"blob_sha={candidate.blob_sha}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish and verify AIHOT candidates in a repository data branch"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    stage = subparsers.add_parser("stage")
    stage.add_argument("--candidate", type=Path, required=True)
    stage.add_argument("--data-root", type=Path, required=True)

    verify = subparsers.add_parser("verify-remote")
    verify.add_argument("--repository", required=True)
    verify.add_argument("--branch", default=DATA_BRANCH)
    verify.add_argument("--path", required=True)
    verify.add_argument("--candidate", type=Path, required=True)
    verify.add_argument("--expected-generated-at", required=True)
    verify.add_argument("--expected-commit-sha", required=True)
    verify.add_argument("--attempts", type=int, default=5)
    verify.add_argument("--interval-seconds", type=float, default=3)

    health = subparsers.add_parser("health")
    health.add_argument("--repository", required=True)
    health.add_argument("--branch", default=DATA_BRANCH)
    health.add_argument("--report-date")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get("GITHUB_TOKEN", "")
    try:
        if args.command == "stage":
            metadata = stage_candidate(args.candidate, args.data_root)
            print(json.dumps(_metadata_as_dict(metadata), ensure_ascii=False))
            return 0

        if not token:
            raise RepositoryDataError("GITHUB_TOKEN is required")

        if args.command == "verify-remote":
            expected_content = args.candidate.read_bytes()
            candidate = verify_repository_candidate_with_retries(
                repository=args.repository,
                branch=args.branch,
                path=args.path,
                token=token,
                expected_content=expected_content,
                expected_generated_at=args.expected_generated_at,
                expected_commit_sha=args.expected_commit_sha,
                attempts=args.attempts,
                interval_seconds=args.interval_seconds,
            )
            _write_outputs(candidate)
            _write_summary(
                title="Repository candidate read-back",
                metadata=candidate.metadata,
                repository=args.repository,
                branch=args.branch,
                commit_sha=candidate.commit_sha,
                blob_sha=candidate.blob_sha,
            )
            print(
                json.dumps(
                    _metadata_as_dict(
                        candidate.metadata,
                        commit_sha=candidate.commit_sha,
                        blob_sha=candidate.blob_sha,
                    ),
                    ensure_ascii=False,
                )
            )
            return 0

        report_day = (
            date.fromisoformat(args.report_date)
            if args.report_date
            else datetime.now(timezone.utc).astimezone(BEIJING_TIMEZONE).date()
        )
        path = f"report-candidate/{report_day.isoformat()}.json"
        candidate = read_repository_candidate(
            repository=args.repository,
            branch=args.branch,
            path=path,
            token=token,
        )
        validate_candidate_for_report(
            _decode_candidate(candidate.content),
            report_day,
            now=datetime.now(timezone.utc),
        )
        _write_summary(
            title="Daily repository candidate readiness",
            metadata=candidate.metadata,
            repository=args.repository,
            branch=args.branch,
            commit_sha=candidate.commit_sha,
            blob_sha=candidate.blob_sha,
        )
        print(
            json.dumps(
                _metadata_as_dict(
                    candidate.metadata,
                    commit_sha=candidate.commit_sha,
                    blob_sha=candidate.blob_sha,
                ),
                ensure_ascii=False,
            )
        )
        return 0
    except (OSError, RepositoryDataError, ValueError) as exc:
        print(f"repository data check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

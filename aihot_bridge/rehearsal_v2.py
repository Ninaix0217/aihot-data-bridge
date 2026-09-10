from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import httpx

from .candidate_v2 import validate_candidate_v2, validated_candidate_bytes
from .candidate_versioning import artifact_sha256, semantic_candidate_hash
from .github_repository_v2 import GitHubGitDataAdapter
from .logical_date import resolve_workflow_dispatch
from .repository_v2 import (
    DATA_BRANCH,
    V2_LATEST_PATH,
    GitObjectAdapter,
    RepositoryV2Error,
    RepositoryV2ErrorReason,
    publish_v2_candidate,
    v2_candidate_path,
)
from .snapshot_v2 import build_live_candidate


@dataclass(frozen=True)
class PathEvidence:
    path: str
    blob_sha: str
    sha256: str


@dataclass(frozen=True)
class RepositorySnapshot:
    head: str
    tree_sha: str
    paths: Mapping[str, str]
    v1_latest: PathEvidence
    v1_current_dated: PathEvidence


def capture_repository_snapshot(adapter: GitObjectAdapter) -> RepositorySnapshot:
    head = adapter.read_head(DATA_BRANCH)
    commit = adapter.read_commit(head)
    tree = dict(adapter.read_tree(commit.tree_sha))
    latest = _path_evidence(adapter, tree, "latest.json", required=True)
    dated_paths = sorted(
        path
        for path in tree
        if path.startswith("report-candidate/") and path.endswith(".json")
    )
    if not dated_paths:
        raise RepositoryV2Error(
            RepositoryV2ErrorReason.REPOSITORY_STATE_INVALID,
            "snapshot-data has no V1 dated candidate",
        )
    current_dated = _path_evidence(
        adapter,
        tree,
        dated_paths[-1],
        required=True,
    )
    assert latest is not None and current_dated is not None
    return RepositorySnapshot(head, commit.tree_sha, tree, latest, current_dated)


def verify_v1_preservation(
    before: RepositorySnapshot,
    after: RepositorySnapshot,
) -> tuple[bool, tuple[str, ...]]:
    all_paths = set(before.paths) | set(after.paths)
    changed = tuple(
        sorted(path for path in all_paths if before.paths.get(path) != after.paths.get(path))
    )
    v1_unchanged = all(path.startswith("v2/") for path in changed)
    required_unchanged = (
        before.v1_latest == after.v1_latest
        and before.v1_current_dated == after.v1_current_dated
    )
    return v1_unchanged and required_unchanged, changed


def run_rehearsal(
    *,
    repository: str,
    target_report_date: str,
    mode: str,
    token: str,
    request_id: str | None = None,
    trigger_source: str | None = None,
    summary_path: Path | None = None,
) -> dict[str, Any]:
    correlation_request_id = _optional_correlation(
        request_id,
        name="request_id",
        max_length=128,
    )
    correlation_source = _optional_correlation(
        trigger_source,
        name="trigger_source",
        max_length=64,
    )
    started_at = datetime.now(timezone.utc)
    trigger = resolve_workflow_dispatch(
        target_report_date=target_report_date,
        mode=mode,
        started_at=started_at,
    )
    build = asyncio.run(build_live_candidate(trigger.target_report_date))
    metadata = validate_candidate_v2(build.payload)
    candidate_bytes = validated_candidate_bytes(build.payload)
    content_hash = semantic_candidate_hash(build.payload)
    artifact_hash = artifact_sha256(build.payload)

    timeout = httpx.Timeout(30.0, connect=10.0)
    with httpx.Client(
        base_url="https://api.github.com",
        timeout=timeout,
        follow_redirects=False,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "aihot-data-bridge-v2-rehearsal",
        },
    ) as client:
        adapter = GitHubGitDataAdapter(client, repository=repository)
        before = capture_repository_snapshot(adapter)
        publication = publish_v2_candidate(adapter, build.payload)
        after = capture_repository_snapshot(adapter)
        preserved, changed_paths = verify_v1_preservation(before, after)
        if not preserved:
            raise RepositoryV2Error(
                RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED,
                "V1 path/blob preservation check failed",
            )
        expected_changed = set()
        if publication.branch_updated:
            if publication.plan.write_dated:
                assert publication.plan.target_path is not None
                expected_changed.add(publication.plan.target_path)
            if publication.plan.write_latest:
                expected_changed.add(V2_LATEST_PATH)
        if set(changed_paths) != expected_changed:
            raise RepositoryV2Error(
                RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED,
                "REHEARSAL_SCOPE_VIOLATION: changed paths do not match the plan",
            )

        target_path = v2_candidate_path(metadata.target_report_date)
        dated = _path_evidence(adapter, after.paths, target_path, required=True)
        assert dated is not None
        dated_payload = _candidate_payload(
            adapter.read_blob(dated.blob_sha),
            path=target_path,
        )
        dated_metadata = validate_candidate_v2(dated_payload)
        if (
            dated_metadata.target_report_date != metadata.target_report_date
            or semantic_candidate_hash(dated_payload) != content_hash
            or artifact_sha256(dated_payload) != artifact_hash
            or dated.sha256 != hashlib.sha256(candidate_bytes).hexdigest()
        ):
            raise RepositoryV2Error(
                RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED,
                "final dated candidate identity/hash mismatch",
            )

        latest = _path_evidence(adapter, after.paths, V2_LATEST_PATH, required=True)
        assert latest is not None
        latest_bytes = adapter.read_blob(latest.blob_sha)
        latest_payload = _candidate_payload(latest_bytes, path=V2_LATEST_PATH)
        latest_metadata = validate_candidate_v2(latest_payload)
        if publication.plan.write_latest and latest_bytes != candidate_bytes:
            raise RepositoryV2Error(
                RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED,
                "final latest is not byte-identical to the accepted dated candidate",
            )

    coverage = build.payload["coverage"]
    result = {
        "trigger_type": trigger.trigger_type.value,
        "identity_status": trigger.identity_status.value,
        "mode": trigger.mode.value if trigger.mode is not None else None,
        "request_id": correlation_request_id,
        "trigger_source": correlation_source,
        "target_report_date": metadata.target_report_date.isoformat(),
        "report_start": metadata.report_start.isoformat(),
        "report_end": metadata.report_end.isoformat(),
        "retrieval_as_of": metadata.retrieval_as_of.isoformat(),
        "generated_at": metadata.generated_at.isoformat(),
        "coverage": {
            channel: {
                "status": coverage[channel]["status"],
                "pages": coverage[channel]["source_range"]["pages_fetched"],
                "proof": coverage[channel]["source_range"]["proof_basis"],
                "oldest": coverage[channel]["source_range"]["oldest_published_at"],
            }
            for channel in ("selected", "all", "paper")
        },
        "candidate_state": "VALID_COMPLETE",
        "content_hash": content_hash,
        "artifact_sha256": artifact_hash,
        "logical_requests": build.logical_requests,
        "duration_seconds": round(build.duration_seconds, 3),
        "repository": {
            "previous_head": publication.original_head,
            "decision": publication.plan.decision.value,
            "reason": publication.plan.reason.value,
            "write_dated": publication.plan.write_dated,
            "write_latest": publication.plan.write_latest,
            "cas_attempts": publication.attempts_used,
            "created_commit": publication.commit_sha,
            "final_head": publication.final_head,
            "dated_blob": dated.blob_sha,
            "latest_blob": latest.blob_sha,
            "latest_target_report_date": latest_metadata.target_report_date.isoformat(),
        },
        "readback": {
            "immutable": "PASS" if publication.branch_updated else "NOT_REQUIRED",
            "final": "PASS",
        },
        "v1_preservation": {
            "result": "PASS",
            "latest_before": asdict(before.v1_latest),
            "latest_after": asdict(after.v1_latest),
            "dated_before": asdict(before.v1_current_dated),
            "dated_after": asdict(after.v1_current_dated),
        },
        "changed_paths": list(changed_paths),
        "consumer": "NOT_CHANGED",
        "pages": "NOT_USED",
    }
    if summary_path is not None:
        _append_summary(summary_path, result)
    return result


def _path_evidence(
    adapter: GitObjectAdapter,
    tree: Mapping[str, str],
    path: str,
    *,
    required: bool,
) -> PathEvidence | None:
    blob_sha = tree.get(path)
    if blob_sha is None:
        if required:
            raise RepositoryV2Error(
                RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED,
                f"required repository path is missing: {path}",
            )
        return None
    content = adapter.read_blob(blob_sha)
    return PathEvidence(path, blob_sha, hashlib.sha256(content).hexdigest())


def _candidate_payload(content: bytes, *, path: str) -> dict[str, Any]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepositoryV2Error(
            RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED,
            f"{path} is not valid UTF-8 JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise RepositoryV2Error(
            RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED,
            f"{path} JSON root is not an object",
        )
    return payload


def _append_summary(path: Path, result: Mapping[str, Any]) -> None:
    repository = result["repository"]
    readback = result["readback"]
    coverage = result["coverage"]
    lines = [
        "## AI HOT V2 Repository Rehearsal",
        "",
        f"- trigger_type: `{result['trigger_type']}`",
        f"- identity_status: `{result['identity_status']}`",
        f"- mode: `{result['mode']}`",
        f"- request_id: `{result['request_id'] or 'NONE'}`",
        f"- trigger_source: `{result['trigger_source'] or 'NONE'}`",
        f"- target_report_date: `{result['target_report_date']}`",
        f"- report_start: `{result['report_start']}`",
        f"- report_end: `{result['report_end']}`",
        f"- retrieval.as_of: `{result['retrieval_as_of']}`",
        f"- generated_at: `{result['generated_at']}`",
        "",
    ]
    for channel in ("selected", "all", "paper"):
        value = coverage[channel]
        lines.extend(
            [
                f"### {channel}",
                f"- status: `{value['status']}`",
                f"- pages: `{value['pages']}`",
                f"- proof: `{value['proof']}`",
                f"- oldest: `{value['oldest']}`",
                "",
            ]
        )
    lines.extend(
        [
            f"- candidate_state: `{result['candidate_state']}`",
            f"- CONTENT_HASH: `{result['content_hash']}`",
            f"- ARTIFACT_SHA256: `{result['artifact_sha256']}`",
            "",
            "### repository",
            f"- previous_head: `{repository['previous_head']}`",
            f"- decision: `{repository['decision']}`",
            f"- reason: `{repository['reason']}`",
            f"- write_dated: `{repository['write_dated']}`",
            f"- write_latest: `{repository['write_latest']}`",
            f"- cas_attempts: `{repository['cas_attempts']}`",
            f"- created_commit: `{repository['created_commit']}`",
            f"- final_head: `{repository['final_head']}`",
            f"- dated_blob: `{repository['dated_blob']}`",
            f"- latest_blob: `{repository['latest_blob']}`",
            "",
            "### readback",
            f"- immutable: `{readback['immutable']}`",
            f"- final: `{readback['final']}`",
            f"- V1 preservation: `{result['v1_preservation']['result']}`",
            f"- changed_paths: `{', '.join(result['changed_paths']) or 'NONE'}`",
            "- Consumer: `NOT_CHANGED`",
            "- Pages: `NOT_USED`",
            "",
        ]
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the dispatch-only V2 repository rehearsal")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--report-date", required=True)
    parser.add_argument("--mode", required=True, choices=("MANUAL", "RECOVERY", "BACKFILL"))
    parser.add_argument("--request-id")
    parser.add_argument("--trigger-source")
    parser.add_argument("--summary", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("V2 rehearsal failed: GITHUB_TOKEN is required", file=sys.stderr)
        return 1
    try:
        result = run_rehearsal(
            repository=args.repository,
            target_report_date=args.report_date,
            mode=args.mode,
            token=token,
            request_id=args.request_id,
            trigger_source=args.trigger_source,
            summary_path=args.summary,
        )
    except Exception as exc:
        if args.summary is not None:
            with args.summary.open("a", encoding="utf-8") as handle:
                handle.write(
                    "## AI HOT V2 Repository Rehearsal\n\n"
                    f"- target_report_date: `{args.report_date}`\n"
                    f"- mode: `{args.mode}`\n"
                    "- result: `FAIL`\n"
                    f"- error: `{str(exc).replace('`', '')}`\n"
                    "- Consumer: `NOT_CHANGED`\n"
                    "- Pages: `NOT_USED`\n"
                )
        print(f"V2 rehearsal failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _optional_correlation(
    value: str | None,
    *,
    name: str,
    max_length: int,
) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > max_length:
        raise ValueError(f"{name} exceeds its observability bound")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", value) is None:
        raise ValueError(f"{name} contains unsafe observability characters")
    return value


if __name__ == "__main__":
    raise SystemExit(main())

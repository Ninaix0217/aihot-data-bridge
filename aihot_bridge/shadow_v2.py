from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import httpx

from .candidate_v2 import (
    CandidateCompletenessState,
    evaluate_candidate_completeness,
    validate_candidate_v2,
    validated_candidate_bytes,
)
from .candidate_versioning import artifact_sha256, semantic_candidate_hash
from .logical_date import resolve_scheduled_trigger
from .retrieval_v2 import CandidateBuildResult
from .snapshot_v2 import build_live_candidate
from .timefields import parse_timestamp


class StartedAtSource(str, Enum):
    ACTIONS_RUN_API = "ACTIONS_RUN_API"
    RUNNER_FALLBACK = "RUNNER_FALLBACK"


@dataclass(frozen=True)
class StartedAtObservation:
    started_at: datetime
    source: StartedAtSource
    api_error: str | None = None


CandidateBuilder = Callable[[date], Awaitable[CandidateBuildResult]]


def observe_started_at(
    client: httpx.Client,
    *,
    repository: str,
    run_id: str,
    runner_fallback_started_at: str,
) -> StartedAtObservation:
    """Prefer the Actions run timestamp and explicitly label any fallback."""
    fallback = parse_timestamp(runner_fallback_started_at)
    if fallback is None:
        raise ValueError("runner fallback started_at must be timezone-aware ISO 8601")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must use owner/name format")
    if not run_id.isdigit():
        raise ValueError("run_id must be numeric")

    try:
        response = client.get(f"/repos/{repository}/actions/runs/{run_id}")
        if response.status_code != 200:
            return StartedAtObservation(
                fallback,
                StartedAtSource.RUNNER_FALLBACK,
                f"Actions run API returned HTTP {response.status_code}",
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Actions run response root is not an object")
        observed = parse_timestamp(payload.get("run_started_at"))
        if observed is None:
            raise ValueError("Actions run response has no valid run_started_at")
        return StartedAtObservation(observed, StartedAtSource.ACTIONS_RUN_API)
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
        return StartedAtObservation(
            fallback,
            StartedAtSource.RUNNER_FALLBACK,
            f"Actions run API unavailable: {exc}",
        )


def run_shadow(
    *,
    event_schedule: str,
    started_at: StartedAtObservation,
    summary_path: Path | None = None,
    candidate_builder: CandidateBuilder | None = None,
) -> dict[str, Any]:
    """Resolve a scheduled D and build a validated candidate without publishing."""
    trigger = resolve_scheduled_trigger(
        event_schedule=event_schedule,
        started_at=started_at.started_at,
    )
    builder = candidate_builder or build_live_candidate
    build = asyncio.run(builder(trigger.target_report_date))
    metadata = validate_candidate_v2(build.payload)
    evaluation = evaluate_candidate_completeness(build.payload)
    if evaluation.state is not CandidateCompletenessState.VALID_COMPLETE:
        raise ValueError("candidate completeness evaluator did not return VALID_COMPLETE")
    candidate_bytes = validated_candidate_bytes(build.payload)

    coverage = build.payload["coverage"]
    result: dict[str, Any] = {
        "trigger": {
            "event_schedule": trigger.event_schedule,
            "pass": trigger.producer_pass.value,
            "identity_status": trigger.identity_status.value,
            "scheduled_for": _format_utc(trigger.scheduled_for),
            "started_at": _format_utc(trigger.started_at),
            "started_at_source": started_at.source.value,
            "started_at_api_error": started_at.api_error,
            "schedule_lag_seconds": trigger.schedule_lag_seconds,
            "target_report_date": trigger.target_report_date.isoformat(),
        },
        "report": {
            "report_start": metadata.report_start.isoformat(),
            "report_end": metadata.report_end.isoformat(),
        },
        "retrieval": {
            "as_of": metadata.retrieval_as_of.isoformat(),
            "generated_at": metadata.generated_at.isoformat(),
        },
        "coverage": {
            channel: _coverage_summary(coverage[channel])
            for channel in ("selected", "all", "paper")
        },
        "supplementary": {
            channel: {"status": coverage[channel]["status"]}
            for channel in ("hot_topics", "daily")
        },
        "candidate": {
            "state": evaluation.state.value,
            "raw_items": build.payload["summary"]["raw_items"],
            "deduplicated_items": build.payload["summary"]["deduplicated_items"],
            "content_hash": semantic_candidate_hash(build.payload),
            "artifact_sha256": artifact_sha256(build.payload),
        },
        "upstream": {
            "logical_requests": build.logical_requests,
            "duration_seconds": round(build.duration_seconds, 3),
        },
        "repository": {"write_attempted": "NO"},
        "v1": "NOT_TOUCHED",
        "consumer": "NOT_TOUCHED",
        "pages": "NOT_USED",
    }
    if result["candidate"]["artifact_sha256"] != _sha256(candidate_bytes):
        raise ValueError("artifact SHA-256 helper does not match validated bytes")
    if summary_path is not None:
        _append_summary(summary_path, result)
    return result


def _coverage_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    source_range = value["source_range"]
    return {
        "status": value["status"],
        "pages_fetched": source_range["pages_fetched"],
        "proof_basis": source_range["proof_basis"],
        "oldest_published_at": source_range["oldest_published_at"],
        "source_range_state": source_range["state"],
    }


def _format_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _append_summary(path: Path, result: Mapping[str, Any]) -> None:
    trigger = result["trigger"]
    report = result["report"]
    retrieval = result["retrieval"]
    coverage = result["coverage"]
    candidate = result["candidate"]
    upstream = result["upstream"]
    lines = [
        "## AI HOT V2 Scheduled Shadow",
        "",
        "### Trigger",
        f"- event_schedule: `{trigger['event_schedule']}`",
        f"- pass: `{trigger['pass']}`",
        f"- identity_status: `{trigger['identity_status']}`",
        f"- scheduled_for: `{trigger['scheduled_for']}`",
        f"- started_at: `{trigger['started_at']}`",
        f"- started_at_source: `{trigger['started_at_source']}`",
        f"- schedule_lag_seconds: `{trigger['schedule_lag_seconds']}`",
        f"- target_report_date: `{trigger['target_report_date']}`",
        "",
        "### Report",
        f"- report_start: `{report['report_start']}`",
        f"- report_end: `{report['report_end']}`",
        "",
        "### Retrieval",
        f"- retrieval.as_of: `{retrieval['as_of']}`",
        f"- generated_at: `{retrieval['generated_at']}`",
        "",
    ]
    for channel in ("selected", "all", "paper"):
        value = coverage[channel]
        lines.extend(
            [
                f"### {channel}",
                f"- status: `{value['status']}`",
                f"- pages_fetched: `{value['pages_fetched']}`",
                f"- proof_basis: `{value['proof_basis']}`",
                f"- oldest_published_at: `{value['oldest_published_at']}`",
                f"- source_range: `{value['source_range_state']}`",
                "",
            ]
        )
    supplementary = result["supplementary"]
    lines.extend(
        [
            "### supplementary",
            f"- hot_topics status: `{supplementary['hot_topics']['status']}`",
            f"- daily status: `{supplementary['daily']['status']}`",
            "",
            "### Candidate",
            f"- state: `{candidate['state']}`",
            f"- raw_items: `{candidate['raw_items']}`",
            f"- deduplicated_items: `{candidate['deduplicated_items']}`",
            f"- CONTENT_HASH: `{candidate['content_hash']}`",
            f"- ARTIFACT_SHA256: `{candidate['artifact_sha256']}`",
            f"- HTTP logical page requests: `{upstream['logical_requests']}`",
            f"- wall clock duration_seconds: `{upstream['duration_seconds']}`",
            "",
            "### Isolation",
            "- Repository WRITE_ATTEMPTED: `NO`",
            "- V1: `NOT_TOUCHED`",
            "- Consumer: `NOT_TOUCHED`",
            "- Pages: `NOT_USED`",
            "",
        ]
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def _append_failure_summary(
    path: Path,
    *,
    event_schedule: str,
    started_at: StartedAtObservation | None,
    error: Exception,
) -> None:
    observed = _format_utc(started_at.started_at) if started_at else "UNAVAILABLE"
    source = started_at.source.value if started_at else "UNAVAILABLE"
    trigger = None
    if started_at is not None:
        try:
            trigger = resolve_scheduled_trigger(
                event_schedule=event_schedule,
                started_at=started_at.started_at,
            )
        except Exception:
            trigger = None
    lines = [
        "## AI HOT V2 Scheduled Shadow",
        "",
        "### Trigger",
        f"- event_schedule: `{event_schedule}`",
        f"- pass: `{trigger.producer_pass.value if trigger else 'UNRESOLVED'}`",
        "- identity_status: `"
        f"{trigger.identity_status.value if trigger else 'UNRESOLVED'}`",
        f"- scheduled_for: `{_format_utc(trigger.scheduled_for) if trigger else 'UNRESOLVED'}`",
        f"- started_at: `{observed}`",
        f"- started_at_source: `{source}`",
        "- schedule_lag_seconds: `"
        f"{trigger.schedule_lag_seconds if trigger else 'UNRESOLVED'}`",
        "- target_report_date: `"
        f"{trigger.target_report_date.isoformat() if trigger else 'UNRESOLVED'}`",
        "- result: `FAIL_CLOSED`",
        f"- error: `{str(error).replace('`', '')}`",
        "",
        "### Isolation",
        "- Repository WRITE_ATTEMPTED: `NO`",
        "- V1: `NOT_TOUCHED`",
        "- Consumer: `NOT_TOUCHED`",
        "- Pages: `NOT_USED`",
        "",
    ]
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the schedule-only V2 shadow")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--event-schedule", required=True)
    parser.add_argument("--runner-fallback-started-at", required=True)
    parser.add_argument("--summary", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.getenv("GITHUB_TOKEN")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "aihot-data-bridge-v2-shadow",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    observation: StartedAtObservation | None = None
    try:
        with httpx.Client(
            base_url="https://api.github.com",
            headers=headers,
            timeout=httpx.Timeout(15.0, connect=10.0),
            follow_redirects=False,
        ) as client:
            observation = observe_started_at(
                client,
                repository=args.repository,
                run_id=args.run_id,
                runner_fallback_started_at=args.runner_fallback_started_at,
            )
        result = run_shadow(
            event_schedule=args.event_schedule,
            started_at=observation,
            summary_path=args.summary,
        )
    except Exception as exc:
        if args.summary is not None:
            _append_failure_summary(
                args.summary,
                event_schedule=args.event_schedule,
                started_at=observation,
                error=exc,
            )
        print(f"V2 scheduled shadow failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

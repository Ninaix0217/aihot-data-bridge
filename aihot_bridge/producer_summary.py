from __future__ import annotations

import argparse
import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .repository_data import (
    RepositoryDataError,
    report_date_for_payload,
    validate_candidate_for_report,
)
from .snapshot import SnapshotRejected, load_snapshot


@dataclass(frozen=True)
class ProducerPass:
    label: str
    scheduled_target: str


SCHEDULED_PASSES = {
    "50 4 * * *": ProducerPass("A", "12:50 Beijing"),
    "10 5 * * *": ProducerPass("B", "13:10 Beijing"),
}


def classify_producer_pass(event_name: str, event_schedule: str) -> ProducerPass:
    """Classify a producer run without inferring a pass from wall-clock time."""
    if event_name.strip() == "workflow_dispatch":
        return ProducerPass("MANUAL", "MANUAL")
    if event_name.strip() == "schedule":
        scheduled_pass = SCHEDULED_PASSES.get(event_schedule.strip())
        if scheduled_pass is not None:
            return scheduled_pass
    return ProducerPass("UNKNOWN", "UNVERIFIED")


def render_producer_summary(
    candidate_path: Path,
    *,
    event_name: str,
    event_schedule: str,
    workflow_started_at: str,
    data_commit_sha: str,
    candidate_blob_sha: str,
    repository_readback_outcome: str,
) -> str:
    """Render observability from an existing candidate without mutating it."""
    content = candidate_path.read_bytes()
    payload = load_snapshot(candidate_path)
    report_day = report_date_for_payload(payload)
    run_pass = classify_producer_pass(event_name, event_schedule)

    window_complete = True
    window_error = ""
    try:
        metadata = validate_candidate_for_report(payload, report_day)
    except RepositoryDataError as exc:
        window_complete = False
        window_error = str(exc)
        metadata = None

    window = payload["window"]
    item_summary = payload["summary"]
    generated_at = metadata.generated_at if metadata else payload["generated_at"]
    report_date = metadata.report_date if metadata else report_day.isoformat()
    candidate_start = metadata.window_from if metadata else window["from"]
    candidate_end = metadata.window_to if metadata else window["to"]
    raw_items = metadata.raw_items if metadata else item_summary["raw_items"]
    deduplicated_items = (
        metadata.deduplicated_items
        if metadata
        else item_summary["deduplicated_items"]
    )
    readback = (
        "PASS" if repository_readback_outcome.strip().lower() == "success" else "FAIL"
    )

    lines = [
        "## AI HOT Daily Producer",
        "",
        f"- Pass: `{run_pass.label}`",
        f"- Scheduled target: `{run_pass.scheduled_target}`",
        f"- Workflow started_at: `{_value(workflow_started_at)}`",
        f"- generated_at: `{generated_at}`",
        f"- report_date: `{report_date}`",
        "",
        f"- candidate_start: `{candidate_start}`",
        f"- candidate_end: `{candidate_end}`",
        f"- candidate_hours: `{window['hours']}`",
        f"- WINDOW_COMPLETE: `{str(window_complete).lower()}`",
    ]
    if window_error:
        lines.append(f"- WINDOW_ERROR: `{_table_value(window_error)}`")

    lines.extend(
        [
            "",
            "| channel | status | source | items |",
            "| --- | --- | --- | ---: |",
        ]
    )
    coverage = payload["coverage"]
    for channel in ("selected", "all", "paper", "hot_topics", "daily"):
        entry: dict[str, Any] = coverage[channel]
        lines.append(
            f"| {channel} | {_table_value(entry['status'])} | "
            f"{_table_value(entry.get('source'))} | {entry['items']} |"
        )

    lines.extend(
        [
            "",
            f"- raw_items: `{raw_items}`",
            f"- deduplicated_items: `{deduplicated_items}`",
            f"- snapshot-data commit: `{_value(data_commit_sha)}`",
            f"- candidate blob SHA: `{_value(candidate_blob_sha)}`",
            f"- content SHA-256: `{hashlib.sha256(content).hexdigest()}`",
            f"- repository readback: `{readback}`",
            "- Pages: `diagnostic only`",
            "",
        ]
    )
    return "\n".join(lines)


def write_producer_summary(
    candidate_path: Path,
    summary_path: Path,
    **kwargs: str,
) -> str:
    rendered = render_producer_summary(candidate_path, **kwargs)
    with summary_path.open("a", encoding="utf-8") as handle:
        handle.write(rendered)
    return rendered


def _value(value: Any) -> str:
    text = str(value).strip() if value is not None else ""
    return text or "UNAVAILABLE"


def _table_value(value: Any) -> str:
    return _value(value).replace("|", "\\|").replace("\n", " ")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write the daily producer run summary")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--event-schedule", default="")
    parser.add_argument("--workflow-started-at", default="")
    parser.add_argument("--data-commit-sha", default="")
    parser.add_argument("--candidate-blob-sha", default="")
    parser.add_argument("--repository-readback-outcome", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    try:
        rendered = render_producer_summary(
            args.candidate,
            event_name=args.event_name,
            event_schedule=args.event_schedule,
            workflow_started_at=args.workflow_started_at,
            data_commit_sha=args.data_commit_sha,
            candidate_blob_sha=args.candidate_blob_sha,
            repository_readback_outcome=args.repository_readback_outcome,
        )
        if summary_path:
            with Path(summary_path).open("a", encoding="utf-8") as handle:
                handle.write(rendered)
        else:
            print(rendered, end="")
        return 0
    except (OSError, SnapshotRejected, RepositoryDataError, ValueError) as exc:
        print(f"producer summary failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

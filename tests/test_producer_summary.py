from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from aihot_bridge.producer_summary import (
    ProducerPass,
    classify_producer_pass,
    render_producer_summary,
    write_producer_summary,
)
from tests.test_snapshot import snapshot_payload


def candidate_payload() -> dict:
    payload = deepcopy(snapshot_payload())
    payload["generated_at"] = "2026-08-20T05:10:00Z"
    payload["window"] = {
        "type": "rolling_candidate",
        "hours": 30,
        "from": "2026-08-18T23:10:00Z",
        "to": "2026-08-20T05:10:00Z",
    }
    return payload


def write_candidate(path: Path) -> bytes:
    content = (
        json.dumps(candidate_payload(), ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    path.write_bytes(content)
    return content


@pytest.mark.parametrize(
    ("event_name", "event_schedule", "expected"),
    [
        ("schedule", "50 4 * * *", ProducerPass("A", "12:50 Beijing")),
        ("schedule", "10 5 * * *", ProducerPass("B", "13:10 Beijing")),
        ("workflow_dispatch", "", ProducerPass("MANUAL", "MANUAL")),
        ("schedule", "11 5 * * *", ProducerPass("UNKNOWN", "UNVERIFIED")),
    ],
)
def test_pass_classification_uses_only_event_schedule(
    event_name: str,
    event_schedule: str,
    expected: ProducerPass,
):
    assert classify_producer_pass(event_name, event_schedule) == expected


def test_summary_reuses_candidate_metadata_without_changing_candidate_bytes(
    tmp_path: Path,
):
    candidate = tmp_path / "candidate.json"
    original = write_candidate(candidate)

    rendered = render_producer_summary(
        candidate,
        event_name="schedule",
        event_schedule="10 5 * * *",
        workflow_started_at="2026-08-20T05:09:40Z",
        data_commit_sha="c" * 40,
        candidate_blob_sha="b" * 40,
        repository_readback_outcome="success",
    )

    assert candidate.read_bytes() == original
    assert "Pass: `B`" in rendered
    assert "Scheduled target: `13:10 Beijing`" in rendered
    assert "candidate_hours: `30`" in rendered
    assert "WINDOW_COMPLETE: `true`" in rendered
    assert "repository readback: `PASS`" in rendered
    assert hashlib.sha256(original).hexdigest() in rendered
    for channel in ("selected", "all", "paper", "hot_topics", "daily"):
        assert f"| {channel} |" in rendered


def test_summary_write_failure_cannot_modify_formal_candidate(tmp_path: Path):
    candidate = tmp_path / "report-candidate" / "2026-08-20.json"
    candidate.parent.mkdir(parents=True)
    original = write_candidate(candidate)
    invalid_summary_path = tmp_path / "summary-directory"
    invalid_summary_path.mkdir()

    with pytest.raises(OSError):
        write_producer_summary(
            candidate,
            invalid_summary_path,
            event_name="workflow_dispatch",
            event_schedule="",
            workflow_started_at="2026-08-20T05:09:40Z",
            data_commit_sha="c" * 40,
            candidate_blob_sha="b" * 40,
            repository_readback_outcome="success",
        )

    assert candidate.read_bytes() == original

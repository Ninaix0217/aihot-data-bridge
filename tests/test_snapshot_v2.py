from __future__ import annotations

import json
from pathlib import Path

from aihot_bridge.candidate_v2 import validate_candidate_v2
from aihot_bridge.retrieval_v2 import CandidateBuildResult
from aihot_bridge import snapshot_v2
from tests.v2_fixtures import complete_candidate_payload


def test_generation_requires_explicit_report_date_and_output(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["snapshot_v2"])

    assert snapshot_v2.main() == 1
    assert "--report-date and --output are required" in capsys.readouterr().err


def test_local_cli_writes_only_validated_v2_artifact(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    payload = complete_candidate_payload()

    async def fake_build(_report_day):
        return CandidateBuildResult(payload, logical_requests=3, duration_seconds=0.25)

    output = tmp_path / "dist-v2" / "candidate.json"
    monkeypatch.setattr(snapshot_v2, "build_live_candidate", fake_build)
    monkeypatch.setattr(
        "sys.argv",
        [
            "snapshot_v2",
            "--report-date",
            "2026-09-08",
            "--output",
            str(output),
        ],
    )

    assert snapshot_v2.main() == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert validate_candidate_v2(written).target_report_date.isoformat() == "2026-09-08"
    summary = json.loads(capsys.readouterr().out)
    assert summary["state"] == "VALID_COMPLETE"
    assert summary["logical_requests"] == 3


def test_local_cli_does_not_write_incomplete_candidate(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    payload = complete_candidate_payload()
    source_range = payload["coverage"]["selected"]["source_range"]
    source_range["state"] = "INCOMPLETE"
    source_range["query_verified"] = False

    async def fake_build(_report_day):
        return CandidateBuildResult(payload, logical_requests=3, duration_seconds=0.25)

    output = tmp_path / "candidate.json"
    monkeypatch.setattr(snapshot_v2, "build_live_candidate", fake_build)
    monkeypatch.setattr(
        "sys.argv",
        [
            "snapshot_v2",
            "--report-date",
            "2026-09-08",
            "--output",
            str(output),
        ],
    )

    assert snapshot_v2.main() == 1
    assert not output.exists()
    assert "SOURCE_RANGE_INCOMPLETE" in capsys.readouterr().err


def test_check_mode_validates_existing_candidate(tmp_path: Path, monkeypatch, capsys):
    candidate = tmp_path / "candidate.json"
    candidate.write_text(
        json.dumps(complete_candidate_payload(), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["snapshot_v2", "--check", str(candidate)])

    assert snapshot_v2.main() == 0
    assert json.loads(capsys.readouterr().out)["state"] == "VALID_COMPLETE"

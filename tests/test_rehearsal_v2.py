from __future__ import annotations

from pathlib import Path

from aihot_bridge.rehearsal_v2 import (
    PathEvidence,
    RepositorySnapshot,
    _append_summary,
    verify_v1_preservation,
)


def snapshot(paths: dict[str, str], *, head: str = "head") -> RepositorySnapshot:
    return RepositorySnapshot(
        head=head,
        tree_sha="tree",
        paths=paths,
        v1_latest=PathEvidence("latest.json", paths["latest.json"], "latest-hash"),
        v1_current_dated=PathEvidence(
            "report-candidate/2026-09-10.json",
            paths["report-candidate/2026-09-10.json"],
            "dated-hash",
        ),
    )


def test_v1_preservation_accepts_only_v2_path_changes():
    before = snapshot(
        {
            "latest.json": "v1-latest",
            "report-candidate/2026-09-10.json": "v1-dated",
        }
    )
    after = snapshot(
        {
            **before.paths,
            "v2/latest.json": "v2-blob",
            "v2/report-candidate/2026-09-10.json": "v2-blob",
        },
        head="new-head",
    )

    preserved, changed = verify_v1_preservation(before, after)

    assert preserved
    assert changed == (
        "v2/latest.json",
        "v2/report-candidate/2026-09-10.json",
    )


def test_v1_preservation_rejects_non_v2_blob_change():
    before = snapshot(
        {
            "latest.json": "v1-latest",
            "report-candidate/2026-09-10.json": "v1-dated",
        }
    )
    changed_paths = dict(before.paths)
    changed_paths["latest.json"] = "mutated"
    after = RepositorySnapshot(
        head="new-head",
        tree_sha="new-tree",
        paths=changed_paths,
        v1_latest=PathEvidence("latest.json", "mutated", "mutated-hash"),
        v1_current_dated=before.v1_current_dated,
    )

    preserved, changed = verify_v1_preservation(before, after)

    assert not preserved
    assert changed == ("latest.json",)


def test_step_summary_contains_required_repository_evidence(tmp_path: Path):
    path = tmp_path / "summary.md"
    result = {
        "trigger_type": "WORKFLOW_DISPATCH",
        "identity_status": "EXPLICIT",
        "mode": "MANUAL",
        "target_report_date": "2026-09-10",
        "report_start": "2026-09-09T04:00:00+00:00",
        "report_end": "2026-09-10T04:00:00+00:00",
        "retrieval_as_of": "2026-09-10T05:00:00+00:00",
        "generated_at": "2026-09-10T05:01:00+00:00",
        "coverage": {
            channel: {
                "status": "ok",
                "pages": 1,
                "proof": "CROSSED_REPORT_START",
                "oldest": "2026-09-09T03:59:59Z",
            }
            for channel in ("selected", "all", "paper")
        },
        "candidate_state": "VALID_COMPLETE",
        "content_hash": "content",
        "artifact_sha256": "artifact",
        "repository": {
            "previous_head": "old",
            "decision": "ACCEPT_NEW",
            "reason": "NO_EXISTING_CANDIDATE",
            "write_dated": True,
            "write_latest": True,
            "cas_attempts": 1,
            "created_commit": "commit",
            "final_head": "commit",
            "dated_blob": "blob",
            "latest_blob": "blob",
        },
        "readback": {"immutable": "PASS", "final": "PASS"},
        "v1_preservation": {"result": "PASS"},
        "changed_paths": [
            "v2/latest.json",
            "v2/report-candidate/2026-09-10.json",
        ],
    }

    _append_summary(path, result)
    rendered = path.read_text(encoding="utf-8")

    assert "AI HOT V2 Repository Rehearsal" in rendered
    assert "identity_status: `EXPLICIT`" in rendered
    assert "immutable: `PASS`" in rendered
    assert "final: `PASS`" in rendered
    assert "V1 preservation: `PASS`" in rendered
    assert "Consumer: `NOT_CHANGED`" in rendered
    assert "Pages: `NOT_USED`" in rendered

from copy import deepcopy

import pytest

from aihot_bridge.candidate_v2 import (
    CandidateCompletenessState, CandidateV2Error,
    evaluate_candidate_completeness, validated_candidate_bytes,
)
from aihot_bridge.candidate_versioning import compare_candidates, CandidateDecision
from tests.v2_fixtures import complete_candidate_payload


def partial_payload():
    payload = complete_candidate_payload()
    selected = payload["coverage"]["selected"]
    selected["status"] = "partial"
    selected["source_range"]["state"] = "INCOMPLETE"
    selected["source_range"]["invalid_published_at_items"] = 2
    return payload


def test_partial_is_explicit_never_complete_and_never_mutates_proof():
    payload = partial_payload()
    before = deepcopy(payload)
    assert evaluate_candidate_completeness(payload).state is CandidateCompletenessState.INVALID
    with pytest.raises(CandidateV2Error):
        validated_candidate_bytes(payload)
    assert evaluate_candidate_completeness(payload, allow_partial=True).state is CandidateCompletenessState.VALID_PARTIAL
    assert validated_candidate_bytes(payload, allow_partial=True)
    assert payload == before


@pytest.mark.parametrize("field,value", [
    ("query_verified", False), ("ordering_verified", False),
    ("page_metadata_verified", False), ("repeated_cursor", True),
    ("max_pages_reached", True), ("cursor_exhausted", False),
])
def test_partial_does_not_excuse_other_proof_failures(field, value):
    payload = partial_payload()
    payload["coverage"]["selected"]["source_range"][field] = value
    assert evaluate_candidate_completeness(payload, allow_partial=True).state is CandidateCompletenessState.INVALID


@pytest.mark.parametrize("source,status", [("rss", "fallback"), (None, "failed")])
def test_partial_cannot_admit_failed_or_rss_source(source, status):
    payload = partial_payload()
    payload["coverage"]["selected"].update(source=source, status=status)
    assert evaluate_candidate_completeness(payload, allow_partial=True).state is CandidateCompletenessState.INVALID


@pytest.mark.parametrize("stamp", [None, "not-time", "2026-09-08T04:00:00Z"])
def test_unknown_malformed_or_out_of_window_formal_item_still_rejected(stamp):
    payload = partial_payload()
    payload["items"][0]["published_at"] = stamp
    assert evaluate_candidate_completeness(payload, allow_partial=True).state is CandidateCompletenessState.INVALID


def test_partial_cannot_replace_complete_with_existing_publication_policy():
    old = complete_candidate_payload()
    new = partial_payload()
    new["generated_at"] = "2026-09-08T07:01:00Z"
    new["retrieval"]["as_of"] = "2026-09-08T07:00:00Z"
    assert compare_candidates(old, new).decision is CandidateDecision.KEEP_EXISTING


def test_allow_partial_leaves_complete_artifact_bytes_unchanged():
    payload = complete_candidate_payload()
    assert validated_candidate_bytes(payload) == validated_candidate_bytes(payload, allow_partial=True)


@pytest.mark.parametrize("field,value", [
    ("generated_at", "2026-09-08T04:59:59Z"),
    ("target_report_date", "2026-09-09"),
])
def test_partial_still_rejects_identity_and_time_tampering(field, value):
    payload = partial_payload()
    payload[field] = value
    assert evaluate_candidate_completeness(payload, allow_partial=True).state is CandidateCompletenessState.INVALID


def test_partial_still_requires_closed_window():
    payload = partial_payload()
    payload["retrieval"]["as_of"] = "2026-09-08T03:59:59Z"
    assert evaluate_candidate_completeness(payload, allow_partial=True).state is CandidateCompletenessState.INVALID


def test_local_partial_cli_is_opt_in_and_check_roundtrips(tmp_path, monkeypatch, capsys):
    import json
    from aihot_bridge import snapshot_v2
    from aihot_bridge.retrieval_v2 import CandidateBuildResult

    payload = partial_payload()
    original = deepcopy(payload)
    output = tmp_path / "candidate.json"

    async def builder(_day):
        return CandidateBuildResult(payload, 7, 0.1)

    monkeypatch.setattr(snapshot_v2, "build_live_candidate", builder)
    arguments = ["snapshot_v2", "--report-date", "2026-09-08", "--output", str(output)]
    monkeypatch.setattr("sys.argv", arguments)
    assert snapshot_v2.main() == 1
    assert not output.exists()
    capsys.readouterr()

    monkeypatch.setattr("sys.argv", arguments + ["--allow-partial"])
    assert snapshot_v2.main() == 0
    assert json.loads(capsys.readouterr().out)["state"] == "VALID_PARTIAL"
    assert json.loads(output.read_text(encoding="utf-8")) == original
    monkeypatch.setattr("sys.argv", ["snapshot_v2", "--check", str(output), "--allow-partial"])
    assert snapshot_v2.main() == 0
    assert json.loads(capsys.readouterr().out)["state"] == "VALID_PARTIAL"
    assert payload == original

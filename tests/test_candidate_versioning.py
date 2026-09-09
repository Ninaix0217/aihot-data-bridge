from __future__ import annotations

from copy import deepcopy
from datetime import date

from aihot_bridge.candidate_versioning import (
    CandidateDecision,
    CandidateDecisionReason,
    artifact_sha256,
    candidate_contract_compatible,
    compare_candidates,
    semantic_candidate_hash,
)
from tests.v2_fixtures import complete_candidate_payload


def set_observation(
    candidate: dict,
    *,
    as_of: str,
    generated_at: str,
) -> dict:
    output = deepcopy(candidate)
    output["retrieval"]["as_of"] = as_of
    output["generated_at"] = generated_at
    return output


def add_item(candidate: dict, item_id: str = "selected-2") -> dict:
    output = deepcopy(candidate)
    item = deepcopy(output["items"][0])
    item["id"] = item_id
    item["title"] = f"Candidate {item_id}"
    item["original_url"] = f"https://example.com/{item_id}"
    item["aihot_url"] = f"https://aihot.virxact.com/items/{item_id}"
    output["items"].append(item)
    output["coverage"]["selected"]["items"] += 1
    output["summary"]["raw_items"] += 1
    output["summary"]["deduplicated_items"] += 1
    return output


def make_incomplete(candidate: dict) -> dict:
    output = deepcopy(candidate)
    proof = output["coverage"]["selected"]["source_range"]
    proof["query_verified"] = False
    proof["state"] = "INCOMPLETE"
    output["coverage"]["selected"]["status"] = "partial"
    return output


def newer(candidate: dict) -> dict:
    return set_observation(
        candidate,
        as_of="2026-09-08T06:00:00Z",
        generated_at="2026-09-08T06:01:00Z",
    )


def test_no_existing_valid_candidate_is_accepted():
    result = compare_candidates(None, complete_candidate_payload())

    assert result.decision is CandidateDecision.ACCEPT_NEW
    assert result.reason is CandidateDecisionReason.NO_EXISTING_CANDIDATE
    assert result.accepts_new


def test_no_existing_incomplete_candidate_is_rejected():
    result = compare_candidates(None, make_incomplete(complete_candidate_payload()))

    assert result.decision is CandidateDecision.REJECT_NEW
    assert result.reason is CandidateDecisionReason.CANDIDATE_INCOMPLETE


def test_valid_existing_keeps_incomplete_new():
    result = compare_candidates(
        complete_candidate_payload(),
        make_incomplete(newer(complete_candidate_payload())),
    )

    assert result.decision is CandidateDecision.KEEP_EXISTING
    assert result.reason is CandidateDecisionReason.CANDIDATE_INCOMPLETE


def test_older_observation_is_stale_attempt():
    existing = newer(complete_candidate_payload())
    new = complete_candidate_payload()

    result = compare_candidates(existing, new)

    assert result.decision is CandidateDecision.KEEP_EXISTING
    assert result.reason is CandidateDecisionReason.STALE_ATTEMPT


def test_same_as_of_semantic_equality_is_idempotent_noop():
    existing = complete_candidate_payload()
    new = deepcopy(existing)
    new["generated_at"] = "2026-09-08T05:02:00Z"

    result = compare_candidates(existing, new)

    assert result.decision is CandidateDecision.IDEMPOTENT_NOOP
    assert result.reason is CandidateDecisionReason.SAME_AS_OF_EQUIVALENT
    assert result.existing_content_hash == result.new_content_hash
    assert result.existing_artifact_sha256 != result.new_artifact_sha256


def test_same_as_of_different_content_is_conflict():
    existing = complete_candidate_payload()
    new = deepcopy(existing)
    new["items"][0]["title"] = "Corrected title"

    result = compare_candidates(existing, new)

    assert result.decision is CandidateDecision.CONFLICT
    assert result.reason is CandidateDecisionReason.SAME_AS_OF_CONFLICT


def test_newer_observation_with_same_content_is_equivalent_but_fresher():
    existing = complete_candidate_payload()
    new = newer(existing)

    result = compare_candidates(existing, new)

    assert result.decision is CandidateDecision.EQUIVALENT_BUT_FRESHER
    assert result.existing_content_hash == result.new_content_hash
    assert result.existing_artifact_sha256 != result.new_artifact_sha256


def test_newer_observation_with_added_item_replaces():
    existing = complete_candidate_payload()
    new = newer(add_item(existing))

    assert compare_candidates(existing, new).decision is CandidateDecision.REPLACE_WITH_NEW


def test_newer_observation_with_removed_item_replaces():
    existing = add_item(complete_candidate_payload())
    new = newer(complete_candidate_payload())

    assert compare_candidates(existing, new).decision is CandidateDecision.REPLACE_WITH_NEW


def test_newer_observation_with_corrected_title_replaces():
    existing = complete_candidate_payload()
    new = newer(existing)
    new["items"][0]["title"] = "Corrected title"

    assert compare_candidates(existing, new).decision is CandidateDecision.REPLACE_WITH_NEW


def test_newer_observation_with_corrected_in_window_published_at_replaces():
    existing = complete_candidate_payload()
    new = newer(existing)
    new["items"][0]["published_at"] = "2026-09-07T05:00:00Z"

    assert compare_candidates(existing, new).decision is CandidateDecision.REPLACE_WITH_NEW


def test_item_count_and_superset_are_not_dominance_requirements():
    existing = add_item(complete_candidate_payload())
    new = newer(complete_candidate_payload())

    result = compare_candidates(existing, new)

    assert result.decision is CandidateDecision.REPLACE_WITH_NEW
    assert new["summary"]["deduplicated_items"] < existing["summary"]["deduplicated_items"]


def test_target_date_mismatch_is_contract_conflict():
    existing = complete_candidate_payload(report_day=date(2026, 9, 8))
    new = complete_candidate_payload(report_day=date(2026, 9, 9))

    result = compare_candidates(existing, new)

    assert result.decision is CandidateDecision.CONFLICT
    assert result.reason is CandidateDecisionReason.CANDIDATE_CONTRACT_MISMATCH


def test_report_window_mismatch_is_contract_conflict():
    existing = complete_candidate_payload()
    new = deepcopy(existing)
    new["report_window"]["from"] = "2026-09-07T04:00:01Z"

    result = compare_candidates(existing, new)

    assert result.decision is CandidateDecision.CONFLICT
    assert result.reason is CandidateDecisionReason.CANDIDATE_CONTRACT_MISMATCH


def test_retrieval_contract_mismatch_is_contract_conflict():
    existing = complete_candidate_payload()
    new = deepcopy(existing)
    new["retrieval"]["upstream_window"] = "30d"

    assert not candidate_contract_compatible(existing, new)
    result = compare_candidates(existing, new)
    assert result.decision is CandidateDecision.CONFLICT
    assert result.reason is CandidateDecisionReason.CANDIDATE_CONTRACT_MISMATCH


def test_existing_malformed_is_conflict_not_automatic_repair():
    result = compare_candidates({"schema_version": "broken"}, complete_candidate_payload())

    assert result.decision is CandidateDecision.CONFLICT
    assert result.reason is CandidateDecisionReason.EXISTING_CANDIDATE_INVALID


def test_semantic_hash_excludes_observation_times_but_artifact_hash_does_not():
    first = complete_candidate_payload()
    second = newer(first)

    assert semantic_candidate_hash(first) == semantic_candidate_hash(second)
    assert artifact_sha256(first) != artifact_sha256(second)


def test_semantic_hash_is_stable_across_dict_and_item_ordering():
    first = add_item(complete_candidate_payload())
    second = dict(reversed(list(deepcopy(first).items())))
    second["items"].reverse()

    assert semantic_candidate_hash(first) == semantic_candidate_hash(second)
    assert semantic_candidate_hash(first) == semantic_candidate_hash(deepcopy(first))


def test_page_packing_proof_noise_does_not_change_semantic_hash():
    first = complete_candidate_payload()
    second = deepcopy(first)
    proof = second["coverage"]["selected"]["source_range"]
    proof["pages_fetched"] = 17
    proof["oldest_published_at"] = "2026-09-06T04:00:00Z"

    assert semantic_candidate_hash(first) == semantic_candidate_hash(second)
    assert artifact_sha256(first) != artifact_sha256(second)


def test_artifact_hash_is_deterministic_for_same_complete_candidate():
    candidate = complete_candidate_payload()

    assert artifact_sha256(candidate) == artifact_sha256(deepcopy(candidate))

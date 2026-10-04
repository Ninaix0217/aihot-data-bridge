from copy import deepcopy
from datetime import date

import pytest

from aihot_bridge.candidate_v2 import validated_candidate_bytes
from aihot_bridge.candidate_versioning import (
    CandidateDecision, CandidateDecisionReason, artifact_sha256,
    compare_candidates, semantic_candidate_hash,
)
from aihot_bridge.repository_v2 import (
    RepositoryV2Error, RepositoryV2ErrorReason, publish_v2_candidate,
)
from aihot_bridge.retrieval_v2 import CandidateBuildResult
from aihot_bridge import rehearsal_v2
from tests.test_partial_candidate_v2 import partial_payload
from tests.test_repository_v2 import FakeGitObjectAdapter, observation
from tests.v2_fixtures import complete_candidate_payload


DATED = "v2/report-candidate/2026-09-08.json"
LATEST = "v2/latest.json"
V1 = {"latest.json": b"v1", "report-candidate/2026-09-08.json": b"v1 dated"}


def later(payload):
    return observation(payload, "2026-09-08T07:00:00Z", "2026-09-08T07:01:00Z")


def seeded(payload):
    content = validated_candidate_bytes(payload, allow_partial=True)
    return FakeGitObjectAdapter({**V1, DATED: content, LATEST: content})


def test_partial_publication_requires_explicit_opt_in_before_any_adapter_call():
    adapter = FakeGitObjectAdapter(V1)
    with pytest.raises(RepositoryV2Error) as error:
        publish_v2_candidate(adapter, partial_payload())
    assert error.value.reason is RepositoryV2ErrorReason.CANDIDATE_INCOMPLETE
    assert adapter.operations == []


def test_first_partial_can_be_accepted_but_never_relabelled_complete():
    payload = partial_payload()
    adapter = FakeGitObjectAdapter(V1)
    result = publish_v2_candidate(adapter, payload, allow_partial=True)
    assert result.plan.decision is CandidateDecision.ACCEPT_NEW
    assert result.plan.write_dated and result.plan.write_latest
    files = adapter.head_files()
    assert files[DATED] == files[LATEST] == validated_candidate_bytes(payload, allow_partial=True)
    assert all(files[path] == content for path, content in V1.items())
    assert payload["coverage"]["selected"]["source_range"]["state"] == "INCOMPLETE"
    assert adapter.update_calls[0][-1] is False


def test_newer_partial_keeps_existing_complete_without_git_object_creation():
    old = complete_candidate_payload()
    adapter = seeded(old)
    before = adapter.head_files()
    result = publish_v2_candidate(adapter, later(partial_payload()), allow_partial=True)
    assert result.plan.decision is CandidateDecision.KEEP_EXISTING
    assert result.plan.reason is CandidateDecisionReason.QUALITY_REGRESSION
    assert not result.branch_updated
    assert "create_blob" not in adapter.operations
    assert adapter.head_files() == before


def test_default_strict_complete_attempt_can_upgrade_existing_partial():
    adapter = seeded(partial_payload())
    new = later(complete_candidate_payload())
    result = publish_v2_candidate(adapter, new)
    assert result.plan.reason is CandidateDecisionReason.QUALITY_UPGRADE
    assert adapter.head_files()[DATED] == validated_candidate_bytes(new)


def test_older_complete_does_not_replace_later_partial():
    comparison = compare_candidates(later(partial_payload()), complete_candidate_payload())
    assert comparison.decision is CandidateDecision.KEEP_EXISTING
    assert comparison.reason is CandidateDecisionReason.STALE_ATTEMPT


def test_same_as_of_quality_change_is_conflict_not_generated_at_tiebreak():
    comparison = compare_candidates(partial_payload(), complete_candidate_payload())
    assert comparison.decision is CandidateDecision.CONFLICT
    assert comparison.reason is CandidateDecisionReason.SAME_AS_OF_CONFLICT


def test_partial_same_as_of_semantics_are_noop():
    old = partial_payload()
    new = deepcopy(old)
    new["generated_at"] = "2026-09-08T06:00:00Z"
    comparison = compare_candidates(old, new, allow_partial=True)
    assert comparison.decision is CandidateDecision.IDEMPOTENT_NOOP
    assert comparison.existing_content_hash == comparison.new_content_hash
    assert comparison.existing_artifact_sha256 != comparison.new_artifact_sha256


def test_partial_newer_equivalent_is_fresher():
    comparison = compare_candidates(partial_payload(), later(partial_payload()), allow_partial=True)
    assert comparison.decision is CandidateDecision.EQUIVALENT_BUT_FRESHER


def test_unknown_timestamp_counts_are_semantic_but_page_packing_is_not():
    old = partial_payload()
    new = deepcopy(old)
    new["coverage"]["selected"]["source_range"]["pages_fetched"] = 2
    assert semantic_candidate_hash(old, allow_partial=True) == semantic_candidate_hash(new, allow_partial=True)
    new["coverage"]["selected"]["source_range"]["invalid_published_at_items"] = 3
    assert semantic_candidate_hash(old, allow_partial=True) != semantic_candidate_hash(new, allow_partial=True)
    assert compare_candidates(old, new, allow_partial=True).decision is CandidateDecision.CONFLICT


def test_partial_newer_removed_item_is_allowed_not_count_quality_score():
    new = later(partial_payload())
    new["items"] = []
    new["summary"] = {"raw_items": 0, "deduplicated_items": 0}
    new["coverage"]["selected"]["items"] = 0
    comparison = compare_candidates(partial_payload(), new, allow_partial=True)
    assert comparison.decision is CandidateDecision.REPLACE_WITH_NEW
    assert comparison.reason is CandidateDecisionReason.NEWER_PARTIAL_CANDIDATE


def test_partial_backfill_does_not_rewind_latest():
    adapter = seeded(complete_candidate_payload())
    historical = complete_candidate_payload(report_day=date(2026, 9, 7))
    historical["coverage"]["selected"] = deepcopy(partial_payload()["coverage"]["selected"])
    historical["coverage"]["selected"]["source_range"]["oldest_published_at"] = historical["report_window"]["from"]
    before_latest = adapter.head_files()[LATEST]
    result = publish_v2_candidate(adapter, historical, allow_partial=True)
    assert result.plan.write_dated and not result.plan.write_latest
    assert adapter.head_files()[LATEST] == before_latest


def test_partial_race_replans_and_preserves_concurrently_complete_candidate():
    new = partial_payload()
    adapter = FakeGitObjectAdapter(V1)
    complete = validated_candidate_bytes(complete_candidate_payload())
    adapter.races.append(lambda obj: obj.advance_with_files({DATED: complete, LATEST: complete}))
    result = publish_v2_candidate(adapter, later(new), allow_partial=True)
    assert result.attempts_used == 2
    assert result.plan.reason is CandidateDecisionReason.QUALITY_REGRESSION
    assert not result.branch_updated
    assert adapter.head_files()[DATED] == complete
    assert all(not call[-1] for call in adapter.update_calls)


def test_partial_immutable_corruption_stops_before_ref_update():
    adapter = FakeGitObjectAdapter(V1)
    adapter.corrupt_created_blob_read = True
    with pytest.raises(RepositoryV2Error) as error:
        publish_v2_candidate(adapter, partial_payload(), allow_partial=True)
    assert error.value.reason is RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED
    assert adapter.update_calls == []
    assert adapter.head_files() == V1


def test_partial_final_corruption_is_not_success():
    adapter = FakeGitObjectAdapter(V1)
    adapter.corrupt_final_latest = True
    with pytest.raises(RepositoryV2Error) as error:
        publish_v2_candidate(adapter, partial_payload(), allow_partial=True)
    assert error.value.reason is RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED


@pytest.mark.parametrize("case", ["partial_first", "keep_complete", "noop_complete", "noop_partial", "promote_partial"])
def test_full_rehearsal_reports_accepted_artifact_not_rejected_attempt(monkeypatch, tmp_path, case):
    if case == "partial_first":
        adapter, new = FakeGitObjectAdapter(V1), partial_payload()
        accepted = new
    elif case == "keep_complete":
        accepted = complete_candidate_payload()
        adapter, new = seeded(accepted), later(partial_payload())
    elif case == "promote_partial":
        adapter, new = seeded(partial_payload()), later(complete_candidate_payload())
        accepted = new
    else:
        accepted = partial_payload() if case == "noop_partial" else complete_candidate_payload()
        adapter, new = seeded(accepted), deepcopy(accepted)
        new["generated_at"] = "2026-09-08T06:00:00Z"

    async def build(_):
        return CandidateBuildResult(new, 7, 0.25)

    monkeypatch.setattr(rehearsal_v2, "build_live_candidate", build)
    monkeypatch.setattr(rehearsal_v2, "GitHubGitDataAdapter", lambda *args, **kwargs: adapter)
    summary = tmp_path / "summary.md"
    result = rehearsal_v2.run_rehearsal(
        repository="Ninaix0217/aihot-data-bridge",
        target_report_date="2026-09-08", mode="MANUAL", token="fixture-only",
        summary_path=summary, allow_partial=case != "promote_partial",
    )
    assert result["artifact_sha256"] == artifact_sha256(accepted, allow_partial=True)
    assert result["content_hash"] == semantic_candidate_hash(accepted, allow_partial=True)
    assert result["generated_at"] == accepted["generated_at"].replace("Z", "+00:00")
    assert result["attempted_candidate"]["artifact_sha256"] == artifact_sha256(new, allow_partial=True)
    assert result["readback"]["final"] == "PASS"
    assert result["v1_preservation"]["result"] == "PASS"
    assert all(adapter.head_files()[path] == content for path, content in V1.items())
    if case.startswith("noop") or case == "keep_complete":
        assert result["changed_paths"] == []
        assert result["readback"]["immutable"] == "NOT_REQUIRED"
        assert not adapter.update_calls
    else:
        assert result["readback"]["immutable"] == "PASS"
    assert "Attempted candidate" in summary.read_text(encoding="utf-8")

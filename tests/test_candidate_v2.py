from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone

import pytest

from aihot_bridge.candidate_v2 import (
    MAX_BACKFILL_DAYS,
    CandidateCompletenessState,
    CandidateV2Error,
    CandidateV2ErrorReason,
    SourceRangeState,
    evaluate_candidate_completeness,
    validate_backfill_admission,
    validate_candidate_v2,
    validated_candidate_bytes,
)
from tests.v2_fixtures import complete_candidate_payload


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def assert_reason(reason: CandidateV2ErrorReason, call) -> CandidateV2Error:
    with pytest.raises(CandidateV2Error) as caught:
        call()
    assert caught.value.reason is reason
    return caught.value


def test_latest_closed_report_date_is_admitted():
    assert MAX_BACKFILL_DAYS == 4
    validate_backfill_admission(date(2026, 9, 8), utc("2026-09-08T05:00:00Z"))


def test_report_end_exactly_at_retrieval_as_of_is_admitted():
    validate_backfill_admission(date(2026, 9, 8), utc("2026-09-08T04:00:00Z"))


def test_report_window_not_closed_fails_before_retrieval():
    assert_reason(
        CandidateV2ErrorReason.REPORT_WINDOW_NOT_CLOSED,
        lambda: validate_backfill_admission(
            date(2026, 9, 8), utc("2026-09-08T03:59:59Z")
        ),
    )


def test_four_day_backfill_is_admitted_when_retention_guard_passes():
    validate_backfill_admission(date(2026, 9, 4), utc("2026-09-08T05:00:00Z"))


def test_five_day_backfill_exceeds_day_horizon_at_guard_boundary():
    error = assert_reason(
        CandidateV2ErrorReason.BACKFILL_HORIZON_EXCEEDED,
        lambda: validate_backfill_admission(
            date(2026, 9, 3), utc("2026-09-08T04:00:00Z")
        ),
    )
    assert "allowed range is 0..4" in error.detail


def test_retention_guard_is_independently_enforced():
    error = assert_reason(
        CandidateV2ErrorReason.BACKFILL_HORIZON_EXCEEDED,
        lambda: validate_backfill_admission(
            date(2026, 9, 3), utc("2026-09-08T04:00:01Z")
        ),
    )
    assert "six-day retrieval admission guard" in error.detail


def test_complete_candidate_validates():
    payload = complete_candidate_payload()

    metadata = validate_candidate_v2(payload)
    evaluation = evaluate_candidate_completeness(payload)

    assert metadata.target_report_date == date(2026, 9, 8)
    assert evaluation.state is CandidateCompletenessState.VALID_COMPLETE
    assert evaluation.metadata == metadata


def test_candidate_contract_version_is_required():
    payload = complete_candidate_payload()
    payload.pop("producer_contract_version")

    assert_reason(
        CandidateV2ErrorReason.SCHEMA_INVALID,
        lambda: validate_candidate_v2(payload),
    )


def test_primary_query_contract_is_required():
    payload = complete_candidate_payload()
    payload["retrieval"]["primary_queries"]["paper"]["category"] = "news"

    assert_reason(
        CandidateV2ErrorReason.SCHEMA_INVALID,
        lambda: validate_candidate_v2(payload),
    )


def test_empty_but_proven_complete_candidate_is_valid():
    payload = complete_candidate_payload(empty=True)

    metadata = validate_candidate_v2(payload)

    assert metadata.raw_items == 0
    assert metadata.deduplicated_items == 0


def test_report_window_tampering_is_rejected():
    payload = complete_candidate_payload()
    payload["report_window"]["from"] = "2026-09-07T04:00:01Z"

    assert_reason(
        CandidateV2ErrorReason.SCHEMA_INVALID,
        lambda: validate_candidate_v2(payload),
    )


def test_target_report_date_tampering_is_rejected():
    payload = complete_candidate_payload()
    payload["target_report_date"] = "2026-09-09"

    assert_reason(
        CandidateV2ErrorReason.SCHEMA_INVALID,
        lambda: validate_candidate_v2(payload),
    )


def test_generated_at_before_retrieval_as_of_is_rejected():
    payload = complete_candidate_payload()
    payload["generated_at"] = "2026-09-08T04:59:59Z"

    assert_reason(
        CandidateV2ErrorReason.TRUST_INVALID,
        lambda: validate_candidate_v2(payload),
    )


def test_retrieval_as_of_before_report_end_is_rejected():
    payload = complete_candidate_payload()
    payload["retrieval"]["as_of"] = "2026-09-08T03:59:59Z"

    assert_reason(
        CandidateV2ErrorReason.REPORT_WINDOW_NOT_CLOSED,
        lambda: validate_candidate_v2(payload),
    )


@pytest.mark.parametrize(
    "published_at",
    [
        "2026-09-07T03:59:59Z",
        "2026-09-08T04:00:00Z",
        "2026-09-08T04:00:01Z",
        "not-a-timestamp",
    ],
)
def test_formal_item_outside_window_or_invalid_is_rejected(published_at: str):
    payload = complete_candidate_payload()
    payload["items"][0]["published_at"] = published_at

    assert_reason(
        CandidateV2ErrorReason.TRUST_INVALID,
        lambda: validate_candidate_v2(payload),
    )


def test_stored_complete_state_cannot_override_incomplete_proof():
    payload = complete_candidate_payload()
    source_range = payload["coverage"]["selected"]["source_range"]
    source_range["query_verified"] = False

    assert_reason(
        CandidateV2ErrorReason.SCHEMA_INVALID,
        lambda: validate_candidate_v2(payload),
    )


def test_truthful_incomplete_source_range_rejects_formal_candidate():
    payload = complete_candidate_payload()
    source_range = payload["coverage"]["selected"]["source_range"]
    source_range["state"] = SourceRangeState.INCOMPLETE.value
    source_range["query_verified"] = False

    assert_reason(
        CandidateV2ErrorReason.SOURCE_RANGE_INCOMPLETE,
        lambda: validate_candidate_v2(payload),
    )


def test_validated_bytes_are_deterministic_and_do_not_mutate_payload():
    payload = complete_candidate_payload()
    original = deepcopy(payload)

    first = validated_candidate_bytes(payload)
    second = validated_candidate_bytes(payload)

    assert first == second
    assert payload == original

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from aihot_bridge.candidate_v2 import CandidateV2Error, CandidateV2ErrorReason
from aihot_bridge.external_trigger_v2 import (
    CONTROL_PATH,
    CONTROL_REF,
    MAX_EXTERNAL_TRIGGER_AGE,
    BootstrapTrigger,
    ExternalControlError,
    ExternalControlErrorReason,
    ExternalRecoveryTrigger,
    dispatch_plan_for,
    validate_control_scope,
    validate_external_trigger,
    validate_trigger_actor,
)


NOW = datetime.fromisoformat("2026-09-10T05:00:00+00:00")


def valid_payload() -> dict:
    return {
        "schema_version": "aihot-external-trigger/v1",
        "enabled": True,
        "target_report_date": "2026-09-10",
        "mode": "RECOVERY",
        "request_id": "2026-09-10/123e4567-e89b-12d3-a456-426614174000",
        "requested_at": "2026-09-10T04:59:00Z",
        "source": "external-scheduler",
    }


def assert_external_reason(reason, call) -> ExternalControlError:
    with pytest.raises(ExternalControlError) as caught:
        call()
    assert caught.value.reason is reason
    return caught.value


def test_disabled_bootstrap_is_accepted_without_dispatch_identity():
    trigger = validate_external_trigger(
        {
            "schema_version": "aihot-external-trigger/v1",
            "enabled": False,
            "kind": "BOOTSTRAP",
        },
        now=NOW,
    )

    assert trigger == BootstrapTrigger()


def test_valid_recovery_payload_produces_fixed_dispatch_plan():
    trigger = validate_external_trigger(valid_payload(), now=NOW)
    assert isinstance(trigger, ExternalRecoveryTrigger)
    plan = dispatch_plan_for(trigger)

    assert plan.workflow == "v2-rehearsal.yml"
    assert plan.ref == "main"
    assert plan.target_report_date == date(2026, 9, 10)
    assert plan.inputs == {
        "target_report_date": "2026-09-10",
        "mode": "RECOVERY",
        "request_id": valid_payload()["request_id"],
        "trigger_source": "external-scheduler",
    }


@pytest.mark.parametrize("value", [None, "", "2026-09-1", "2026-02-30"])
def test_missing_or_invalid_target_date_is_rejected(value):
    payload = valid_payload()
    if value is None:
        payload.pop("target_report_date")
    else:
        payload["target_report_date"] = value

    assert_external_reason(
        ExternalControlErrorReason.INVALID_EXTERNAL_TRIGGER,
        lambda: validate_external_trigger(payload, now=NOW),
    )


@pytest.mark.parametrize("mode", ["MANUAL", "BACKFILL", "manual", ""])
def test_automated_ingress_allows_recovery_mode_only(mode):
    payload = valid_payload()
    payload["mode"] = mode

    assert_external_reason(
        ExternalControlErrorReason.INVALID_EXTERNAL_TRIGGER,
        lambda: validate_external_trigger(payload, now=NOW),
    )


def test_recovery_mode_is_allowed():
    trigger = validate_external_trigger(valid_payload(), now=NOW)
    assert isinstance(trigger, ExternalRecoveryTrigger)
    assert trigger.mode.value == "RECOVERY"


@pytest.mark.parametrize("requested_at", ["2026-09-10T04:59:00", "not-a-time", None])
def test_naive_or_malformed_requested_at_is_rejected(requested_at):
    payload = valid_payload()
    payload["requested_at"] = requested_at

    assert_external_reason(
        ExternalControlErrorReason.INVALID_EXTERNAL_TRIGGER,
        lambda: validate_external_trigger(payload, now=NOW),
    )


def test_future_requested_at_is_rejected():
    payload = valid_payload()
    payload["requested_at"] = "2026-09-10T05:00:01Z"

    assert_external_reason(
        ExternalControlErrorReason.INVALID_EXTERNAL_TRIGGER,
        lambda: validate_external_trigger(payload, now=NOW),
    )


def test_exact_24_hour_age_is_allowed_but_older_is_stale():
    assert MAX_EXTERNAL_TRIGGER_AGE == timedelta(hours=24)
    payload = valid_payload()
    payload["requested_at"] = "2026-09-09T05:00:00Z"
    assert isinstance(validate_external_trigger(payload, now=NOW), ExternalRecoveryTrigger)

    payload["requested_at"] = "2026-09-09T04:59:59Z"
    assert_external_reason(
        ExternalControlErrorReason.STALE_EXTERNAL_TRIGGER,
        lambda: validate_external_trigger(payload, now=NOW),
    )


def test_unknown_schema_and_malformed_source_are_rejected():
    schema = valid_payload()
    schema["schema_version"] = "aihot-external-trigger/v2"
    source = valid_payload()
    source["source"] = "bad source"

    for payload in (schema, source):
        assert_external_reason(
            ExternalControlErrorReason.INVALID_EXTERNAL_TRIGGER,
            lambda payload=payload: validate_external_trigger(payload, now=NOW),
        )


@pytest.mark.parametrize(
    "request_id",
    ["", " leading", "unsafe?value", "x" * 129],
)
def test_invalid_request_id_is_rejected(request_id):
    payload = valid_payload()
    payload["request_id"] = request_id

    assert_external_reason(
        ExternalControlErrorReason.INVALID_EXTERNAL_TRIGGER,
        lambda: validate_external_trigger(payload, now=NOW),
    )


def test_payload_cannot_control_workflow_repository_or_ref():
    for field, value in (
        ("workflow", "attacker.yml"),
        ("ref", "attacker"),
        ("repository", "other/repo"),
    ):
        payload = valid_payload()
        payload[field] = value
        assert_external_reason(
            ExternalControlErrorReason.INVALID_EXTERNAL_TRIGGER,
            lambda payload=payload: validate_external_trigger(payload, now=NOW),
        )


def test_report_window_must_be_closed_and_backfill_admission_still_applies():
    not_closed = valid_payload()
    not_closed["requested_at"] = "2026-09-10T03:59:00Z"
    with pytest.raises(CandidateV2Error) as caught:
        validate_external_trigger(
            not_closed,
            now=datetime.fromisoformat("2026-09-10T03:59:59+00:00"),
        )
    assert caught.value.reason is CandidateV2ErrorReason.REPORT_WINDOW_NOT_CLOSED

    old = valid_payload()
    old["target_report_date"] = "2026-09-05"
    with pytest.raises(CandidateV2Error) as caught:
        validate_external_trigger(old, now=NOW)
    assert caught.value.reason is CandidateV2ErrorReason.BACKFILL_HORIZON_EXCEEDED


def test_actor_allowlist_accepts_owner_and_records_id():
    assert validate_trigger_actor("Ninaix0217", "12345") == ("Ninaix0217", 12345)
    assert validate_trigger_actor("ninaix0217", None) == ("Ninaix0217", None)


def test_actor_mismatch_is_rejected():
    assert_external_reason(
        ExternalControlErrorReason.UNAUTHORIZED_TRIGGER_ACTOR,
        lambda: validate_trigger_actor("another-user", "999"),
    )


def test_control_scope_allows_exact_single_trigger_file_change():
    validate_control_scope(
        ref=CONTROL_REF,
        before="a" * 40,
        after="b" * 40,
        changed_paths=(CONTROL_PATH,),
        commits=1,
    )


@pytest.mark.parametrize(
    "changes",
    [
        (CONTROL_PATH, "README.md"),
        (".aihot-control/other.json",),
        (),
    ],
)
def test_control_scope_rejects_trigger_plus_unrelated_or_missing_file(changes):
    assert_external_reason(
        ExternalControlErrorReason.CONTROL_SCOPE_VIOLATION,
        lambda: validate_control_scope(
            ref=CONTROL_REF,
            before="a" * 40,
            after="b" * 40,
            changed_paths=changes,
            commits=1,
        ),
    )


def test_control_scope_rejects_wrong_ref_or_multiple_commits():
    for ref, commits in (("refs/heads/main", 1), (CONTROL_REF, 2)):
        assert_external_reason(
            ExternalControlErrorReason.CONTROL_SCOPE_VIOLATION,
            lambda ref=ref, commits=commits: validate_control_scope(
                ref=ref,
                before="a" * 40,
                after="b" * 40,
                changed_paths=(CONTROL_PATH,),
                commits=commits,
            ),
        )

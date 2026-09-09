from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone

import pytest

from aihot_bridge.logical_date import (
    BEIJING_TIMEZONE,
    IDENTITY_RESOLUTION_MAX_LAG,
    DispatchMode,
    IdentityStatus,
    LogicalDateError,
    LogicalDateErrorReason,
    ProducerPass,
    TriggerType,
    logical_report_context,
    report_window_for_date,
    resolve_scheduled_trigger,
    resolve_workflow_dispatch,
)
from aihot_bridge.repository_data import report_window as v1_report_window


PASS_A = "50 4 * * *"
PASS_B = "10 5 * * *"


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def assert_error_reason(reason: LogicalDateErrorReason, call) -> None:
    with pytest.raises(LogicalDateError) as caught:
        call()
    assert caught.value.reason is reason
    assert str(caught.value).startswith(f"{reason.value}:")


def test_report_window_for_date_uses_beijing_noon_and_timezone_aware_utc():
    start, end = report_window_for_date(date(2026, 9, 8))

    assert start == datetime(2026, 9, 7, 12, tzinfo=BEIJING_TIMEZONE)
    assert end == datetime(2026, 9, 8, 12, tzinfo=BEIJING_TIMEZONE)
    assert start.astimezone(timezone.utc) == utc("2026-09-07T04:00:00Z")
    assert end.astimezone(timezone.utc) == utc("2026-09-08T04:00:00Z")
    assert start < end
    assert end - start == timedelta(hours=24)


def test_v1_report_window_wrapper_is_semantically_equivalent():
    report_day = date(2026, 9, 8)

    assert v1_report_window(report_day) == report_window_for_date(report_day)


@pytest.mark.parametrize(
    ("event_schedule", "started_at", "expected_pass", "expected_day", "lag"),
    [
        (PASS_A, "2026-09-08T04:51:00Z", ProducerPass.A, date(2026, 9, 8), 60),
        (PASS_B, "2026-09-08T05:11:00Z", ProducerPass.B, date(2026, 9, 8), 60),
        (PASS_A, "2026-09-08T06:50:00Z", ProducerPass.A, date(2026, 9, 8), 7200),
        (PASS_B, "2026-08-28T15:10:00Z", ProducerPass.B, date(2026, 8, 28), 36000),
        (PASS_B, "2026-08-28T17:20:46Z", ProducerPass.B, date(2026, 8, 28), 43846),
    ],
)
def test_scheduled_trigger_resolves_pass_date_and_lag(
    event_schedule: str,
    started_at: str,
    expected_pass: ProducerPass,
    expected_day: date,
    lag: int,
):
    context = resolve_scheduled_trigger(
        event_schedule=event_schedule,
        started_at=utc(started_at),
    )

    assert context.trigger_type is TriggerType.SCHEDULE
    assert context.producer_pass is expected_pass
    assert context.target_report_date == expected_day
    assert context.schedule_lag_seconds == lag
    assert context.identity_status is IdentityStatus.BOUNDED_CRON_INFERENCE
    assert context.mode is None


def test_delayed_run_crossing_beijing_midnight_keeps_original_report_date():
    context = resolve_scheduled_trigger(
        event_schedule=PASS_B,
        started_at=utc("2026-08-28T17:20:46Z"),
    )

    assert context.started_at.astimezone(BEIJING_TIMEZONE) == datetime(
        2026, 8, 29, 1, 20, 46, tzinfo=BEIJING_TIMEZONE
    )
    assert context.scheduled_for == utc("2026-08-28T05:10:00Z")
    assert context.target_report_date == date(2026, 8, 28)


def test_exactly_18_hours_lag_is_accepted():
    context = resolve_scheduled_trigger(
        event_schedule=PASS_A,
        started_at=utc("2026-09-08T22:50:00Z"),
    )

    assert IDENTITY_RESOLUTION_MAX_LAG == timedelta(hours=18)
    assert context.schedule_lag_seconds == 18 * 60 * 60
    assert context.target_report_date == date(2026, 9, 8)


@pytest.mark.parametrize(
    "started_at",
    [
        "2026-09-08T22:50:01Z",
        "2026-09-09T03:50:00Z",
        "2026-09-08T04:49:00Z",
    ],
)
def test_schedule_outside_inference_window_fails_closed(started_at: str):
    assert_error_reason(
        LogicalDateErrorReason.SCHEDULE_IDENTITY_UNRESOLVED,
        lambda: resolve_scheduled_trigger(
            event_schedule=PASS_A,
            started_at=utc(started_at),
        ),
    )


def test_unknown_cron_fails_closed():
    assert_error_reason(
        LogicalDateErrorReason.UNKNOWN_SCHEDULE,
        lambda: resolve_scheduled_trigger(
            event_schedule="11 5 * * *",
            started_at=utc("2026-09-08T05:11:00Z"),
        ),
    )


def test_naive_started_at_is_rejected():
    assert_error_reason(
        LogicalDateErrorReason.NAIVE_STARTED_AT,
        lambda: resolve_scheduled_trigger(
            event_schedule=PASS_A,
            started_at=datetime(2026, 9, 8, 4, 51),
        ),
    )


@pytest.mark.parametrize(
    ("mode", "expected_mode"),
    [
        ("MANUAL", DispatchMode.MANUAL),
        ("RECOVERY", DispatchMode.RECOVERY),
        ("BACKFILL", DispatchMode.BACKFILL),
    ],
)
def test_workflow_dispatch_is_explicit_and_keeps_pass_separate_from_mode(
    mode: str,
    expected_mode: DispatchMode,
):
    context = resolve_workflow_dispatch(
        target_report_date="2026-09-08",
        mode=mode,
        started_at=utc("2026-09-09T01:00:00Z"),
    )

    assert context.trigger_type is TriggerType.WORKFLOW_DISPATCH
    assert context.producer_pass is ProducerPass.MANUAL
    assert context.mode is expected_mode
    assert context.target_report_date == date(2026, 9, 8)
    assert context.scheduled_for is None
    assert context.schedule_lag_seconds is None
    assert context.identity_status is IdentityStatus.EXPLICIT
    assert context.event_schedule is None


@pytest.mark.parametrize("target", [None, "", "2026-09-8", "2026-02-30"])
def test_workflow_dispatch_requires_valid_target_report_date(target):
    assert_error_reason(
        LogicalDateErrorReason.INVALID_TARGET_REPORT_DATE,
        lambda: resolve_workflow_dispatch(
            target_report_date=target,
            mode="MANUAL",
            started_at=utc("2026-09-09T01:00:00Z"),
        ),
    )


@pytest.mark.parametrize("mode", [None, "", "manual", "RETRY"])
def test_workflow_dispatch_requires_valid_mode(mode):
    assert_error_reason(
        LogicalDateErrorReason.INVALID_DISPATCH_MODE,
        lambda: resolve_workflow_dispatch(
            target_report_date="2026-09-08",
            mode=mode,
            started_at=utc("2026-09-09T01:00:00Z"),
        ),
    )


@pytest.mark.parametrize(
    ("event_schedule", "started_at"),
    [
        (PASS_A, "2026-09-08T04:50:00Z"),
        (PASS_A, "2026-09-08T12:50:00Z"),
        (PASS_B, "2026-09-08T17:10:00Z"),
        (PASS_B, "2026-09-08T23:10:00Z"),
    ],
)
def test_accepted_schedule_context_invariants(event_schedule: str, started_at: str):
    context = resolve_scheduled_trigger(
        event_schedule=event_schedule,
        started_at=utc(started_at),
    )

    assert context.scheduled_for is not None
    assert context.schedule_lag_seconds is not None
    assert 0 <= context.schedule_lag_seconds <= 18 * 60 * 60
    assert context.target_report_date == context.scheduled_for.astimezone(
        BEIJING_TIMEZONE
    ).date()
    report_context = context.report_context
    assert report_context == logical_report_context(context.target_report_date)
    assert report_context.report_start < report_context.report_end
    assert report_context.report_end - report_context.report_start == timedelta(
        hours=24
    )


def test_same_inputs_produce_same_immutable_domain_output():
    inputs = {
        "event_schedule": PASS_B,
        "started_at": utc("2026-08-28T17:20:46Z"),
    }
    first = resolve_scheduled_trigger(**inputs)
    second = resolve_scheduled_trigger(**inputs)

    assert first == second
    assert hash(first) == hash(second)
    with pytest.raises(FrozenInstanceError):
        first.target_report_date = date(2026, 8, 29)


def test_generated_at_is_not_an_identity_input():
    assert "generated_at" not in inspect.signature(resolve_scheduled_trigger).parameters
    assert "generated_at" not in inspect.signature(resolve_workflow_dispatch).parameters


@pytest.mark.parametrize(
    ("event_schedule", "started_at", "expected_pass", "expected_lag"),
    [
        (PASS_A, "2026-08-28T16:56:51Z", ProducerPass.A, 43611),
        (PASS_B, "2026-08-28T17:20:46Z", ProducerPass.B, 43846),
    ],
)
def test_real_incidents_replay_to_original_business_date(
    event_schedule: str,
    started_at: str,
    expected_pass: ProducerPass,
    expected_lag: int,
):
    context = resolve_scheduled_trigger(
        event_schedule=event_schedule,
        started_at=utc(started_at),
    )

    assert context.producer_pass is expected_pass
    assert context.target_report_date == date(2026, 8, 28)
    assert context.schedule_lag_seconds == expected_lag
    assert context.identity_status is IdentityStatus.BOUNDED_CRON_INFERENCE

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo


BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
IDENTITY_RESOLUTION_MAX_LAG = timedelta(hours=18)


class TriggerType(str, Enum):
    SCHEDULE = "SCHEDULE"
    WORKFLOW_DISPATCH = "WORKFLOW_DISPATCH"


class ProducerPass(str, Enum):
    A = "A"
    B = "B"
    MANUAL = "MANUAL"


class DispatchMode(str, Enum):
    MANUAL = "MANUAL"
    RECOVERY = "RECOVERY"
    BACKFILL = "BACKFILL"


class IdentityStatus(str, Enum):
    BOUNDED_CRON_INFERENCE = "BOUNDED_CRON_INFERENCE"
    EXPLICIT = "EXPLICIT"


class LogicalDateErrorReason(str, Enum):
    SCHEDULE_IDENTITY_UNRESOLVED = "SCHEDULE_IDENTITY_UNRESOLVED"
    UNKNOWN_SCHEDULE = "UNKNOWN_SCHEDULE"
    INVALID_TARGET_REPORT_DATE = "INVALID_TARGET_REPORT_DATE"
    INVALID_DISPATCH_MODE = "INVALID_DISPATCH_MODE"
    INVALID_STARTED_AT = "INVALID_STARTED_AT"
    NAIVE_STARTED_AT = "NAIVE_STARTED_AT"


class LogicalDateError(ValueError):
    """A stable, machine-readable logical-date contract failure."""

    def __init__(self, reason: LogicalDateErrorReason, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}")


@dataclass(frozen=True)
class LogicalReportContext:
    target_report_date: date
    report_start: datetime
    report_end: datetime


@dataclass(frozen=True)
class TriggerContext:
    trigger_type: TriggerType
    producer_pass: ProducerPass
    mode: DispatchMode | None
    target_report_date: date
    scheduled_for: datetime | None
    started_at: datetime
    schedule_lag_seconds: int | None
    identity_status: IdentityStatus
    event_schedule: str | None

    @property
    def report_context(self) -> LogicalReportContext:
        return logical_report_context(self.target_report_date)


@dataclass(frozen=True)
class _DailySchedule:
    producer_pass: ProducerPass
    hour_utc: int
    minute_utc: int


_SCHEDULES = {
    "50 4 * * *": _DailySchedule(ProducerPass.A, hour_utc=4, minute_utc=50),
    "10 5 * * *": _DailySchedule(ProducerPass.B, hour_utc=5, minute_utc=10),
}


def report_window_for_date(report_day: date) -> tuple[datetime, datetime]:
    """Return the canonical [D-1 noon, D noon) Asia/Shanghai window."""
    if not isinstance(report_day, date) or isinstance(report_day, datetime):
        raise TypeError("report_day must be a date")
    report_start = datetime.combine(
        report_day - timedelta(days=1),
        time(hour=12),
        tzinfo=BEIJING_TIMEZONE,
    )
    report_end = datetime.combine(
        report_day,
        time(hour=12),
        tzinfo=BEIJING_TIMEZONE,
    )
    return report_start, report_end


def logical_report_context(report_day: date) -> LogicalReportContext:
    report_start, report_end = report_window_for_date(report_day)
    return LogicalReportContext(report_day, report_start, report_end)


def resolve_scheduled_trigger(
    *,
    event_schedule: str,
    started_at: datetime,
) -> TriggerContext:
    """Resolve a known daily UTC cron using bounded, never verified, inference."""
    started_at_utc = _aware_started_at(started_at)
    schedule = _SCHEDULES.get(event_schedule)
    if schedule is None:
        raise LogicalDateError(
            LogicalDateErrorReason.UNKNOWN_SCHEDULE,
            f"unsupported github.event.schedule: {event_schedule!r}",
        )

    scheduled_for = datetime.combine(
        started_at_utc.date(),
        time(hour=schedule.hour_utc, minute=schedule.minute_utc),
        tzinfo=timezone.utc,
    )
    if scheduled_for > started_at_utc:
        scheduled_for -= timedelta(days=1)

    lag = started_at_utc - scheduled_for
    if lag < timedelta(0) or lag > IDENTITY_RESOLUTION_MAX_LAG:
        raise LogicalDateError(
            LogicalDateErrorReason.SCHEDULE_IDENTITY_UNRESOLVED,
            "nearest nominal occurrence is outside the inclusive 18-hour "
            f"inference window (lag_seconds={lag.total_seconds():g})",
        )

    target_report_date = scheduled_for.astimezone(BEIJING_TIMEZONE).date()
    return TriggerContext(
        trigger_type=TriggerType.SCHEDULE,
        producer_pass=schedule.producer_pass,
        mode=None,
        target_report_date=target_report_date,
        scheduled_for=scheduled_for,
        started_at=started_at_utc,
        schedule_lag_seconds=int(lag.total_seconds()),
        identity_status=IdentityStatus.BOUNDED_CRON_INFERENCE,
        event_schedule=event_schedule,
    )


def resolve_workflow_dispatch(
    *,
    target_report_date: date | str | None,
    mode: DispatchMode | str | None,
    started_at: datetime,
) -> TriggerContext:
    """Resolve an explicit manual/recovery/backfill trigger without clock inference."""
    started_at_utc = _aware_started_at(started_at)
    report_day = _parse_target_report_date(target_report_date)
    dispatch_mode = _parse_dispatch_mode(mode)
    return TriggerContext(
        trigger_type=TriggerType.WORKFLOW_DISPATCH,
        producer_pass=ProducerPass.MANUAL,
        mode=dispatch_mode,
        target_report_date=report_day,
        scheduled_for=None,
        started_at=started_at_utc,
        schedule_lag_seconds=None,
        identity_status=IdentityStatus.EXPLICIT,
        event_schedule=None,
    )


def _aware_started_at(value: Any) -> datetime:
    if not isinstance(value, datetime):
        raise LogicalDateError(
            LogicalDateErrorReason.INVALID_STARTED_AT,
            "started_at must be a datetime",
        )
    if value.tzinfo is None or value.utcoffset() is None:
        raise LogicalDateError(
            LogicalDateErrorReason.NAIVE_STARTED_AT,
            "started_at must include a timezone",
        )
    return value.astimezone(timezone.utc)


def _parse_target_report_date(value: date | str | None) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) and len(value) == 10:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.isoformat() == value:
            return parsed
    raise LogicalDateError(
        LogicalDateErrorReason.INVALID_TARGET_REPORT_DATE,
        "target_report_date is required in YYYY-MM-DD format",
    )


def _parse_dispatch_mode(value: DispatchMode | str | None) -> DispatchMode:
    if isinstance(value, DispatchMode):
        return value
    try:
        if not isinstance(value, str):
            raise ValueError
        return DispatchMode(value)
    except ValueError as exc:
        raise LogicalDateError(
            LogicalDateErrorReason.INVALID_DISPATCH_MODE,
            "mode must be one of MANUAL, RECOVERY, or BACKFILL",
        ) from exc

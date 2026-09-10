from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping

from .candidate_v2 import validate_backfill_admission
from .logical_date import DispatchMode
from .timefields import parse_timestamp


EXTERNAL_TRIGGER_SCHEMA = "aihot-external-trigger/v1"
CONTROL_BRANCH = "aihot-scheduler-control"
CONTROL_REF = f"refs/heads/{CONTROL_BRANCH}"
CONTROL_PATH = ".aihot-control/trigger.json"
WRITER_WORKFLOW = "v2-rehearsal.yml"
WRITER_REF = "main"
MAX_EXTERNAL_TRIGGER_AGE = timedelta(hours=24)
ALLOWED_TRIGGER_ACTORS = frozenset({"Ninaix0217"})

_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_ACTIVE_FIELDS = frozenset(
    {
        "schema_version",
        "enabled",
        "target_report_date",
        "mode",
        "request_id",
        "requested_at",
        "source",
    }
)
_BOOTSTRAP_FIELDS = frozenset({"schema_version", "enabled", "kind"})


class ExternalControlErrorReason(str, Enum):
    INVALID_EXTERNAL_TRIGGER = "INVALID_EXTERNAL_TRIGGER"
    STALE_EXTERNAL_TRIGGER = "STALE_EXTERNAL_TRIGGER"
    UNAUTHORIZED_TRIGGER_ACTOR = "UNAUTHORIZED_TRIGGER_ACTOR"
    CONTROL_SCOPE_VIOLATION = "CONTROL_SCOPE_VIOLATION"
    EXTERNAL_DISPATCH_FAILED = "EXTERNAL_DISPATCH_FAILED"


class ExternalControlError(ValueError):
    def __init__(
        self,
        reason: ExternalControlErrorReason,
        detail: str,
        *,
        status_code: int | None = None,
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.status_code = status_code
        super().__init__(f"{reason.value}: {detail}")


@dataclass(frozen=True)
class BootstrapTrigger:
    schema_version: str = EXTERNAL_TRIGGER_SCHEMA
    enabled: bool = False
    kind: str = "BOOTSTRAP"


@dataclass(frozen=True)
class ExternalRecoveryTrigger:
    target_report_date: date
    mode: DispatchMode
    request_id: str
    requested_at: datetime
    source: str


@dataclass(frozen=True)
class ExternalDispatchPlan:
    workflow: str
    ref: str
    target_report_date: date
    mode: DispatchMode
    request_id: str
    trigger_source: str

    @property
    def inputs(self) -> dict[str, str]:
        return {
            "target_report_date": self.target_report_date.isoformat(),
            "mode": self.mode.value,
            "request_id": self.request_id,
            "trigger_source": self.trigger_source,
        }


def validate_trigger_actor(
    actor: str,
    actor_id: str | int | None,
    *,
    allowed_actors: frozenset[str] = ALLOWED_TRIGGER_ACTORS,
) -> tuple[str, int | None]:
    if not isinstance(actor, str) or not actor.strip():
        _error(ExternalControlErrorReason.UNAUTHORIZED_TRIGGER_ACTOR, "actor is missing")
    canonical = next(
        (allowed for allowed in allowed_actors if allowed.casefold() == actor.casefold()),
        None,
    )
    if canonical is None:
        _error(
            ExternalControlErrorReason.UNAUTHORIZED_TRIGGER_ACTOR,
            f"actor {actor!r} is not in the control-plane allowlist",
        )
    parsed_id: int | None = None
    if actor_id not in (None, ""):
        try:
            parsed_id = int(actor_id)
        except (TypeError, ValueError):
            _error(
                ExternalControlErrorReason.UNAUTHORIZED_TRIGGER_ACTOR,
                "actor_id must be a positive integer when present",
            )
        if parsed_id <= 0:
            _error(
                ExternalControlErrorReason.UNAUTHORIZED_TRIGGER_ACTOR,
                "actor_id must be a positive integer when present",
            )
    assert canonical is not None
    return canonical, parsed_id


def validate_control_scope(
    *,
    ref: str,
    before: str,
    after: str,
    changed_paths: tuple[str, ...],
    commits: int,
) -> None:
    if ref != CONTROL_REF:
        _scope_error(f"push ref must be {CONTROL_REF}")
    if not _SHA.fullmatch(before) or not _SHA.fullmatch(after) or before == after:
        _scope_error("push before/after must be distinct full Git commit SHAs")
    if commits != 1:
        _scope_error("control push must contain exactly one commit")
    if changed_paths != (CONTROL_PATH,):
        _scope_error(
            f"control push may change only {CONTROL_PATH}; observed={changed_paths!r}"
        )


def validate_external_trigger(
    payload: Any,
    *,
    now: datetime,
) -> BootstrapTrigger | ExternalRecoveryTrigger:
    if not isinstance(payload, Mapping):
        _invalid("trigger JSON root must be an object")
    if payload.get("schema_version") != EXTERNAL_TRIGGER_SCHEMA:
        _invalid(f"schema_version must be {EXTERNAL_TRIGGER_SCHEMA}")

    enabled = payload.get("enabled")
    if enabled is False:
        if frozenset(payload) != _BOOTSTRAP_FIELDS or payload.get("kind") != "BOOTSTRAP":
            _invalid("disabled trigger must be the exact BOOTSTRAP payload")
        return BootstrapTrigger()
    if enabled is not True:
        _invalid("enabled must be a boolean")
    if frozenset(payload) != _ACTIVE_FIELDS:
        _invalid("active trigger fields do not match the fixed v1 contract")

    mode = payload.get("mode")
    if mode != DispatchMode.RECOVERY.value:
        _invalid("automated external ingress permits mode=RECOVERY only")
    report_day = _report_date(payload.get("target_report_date"))
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        _invalid("request_id must use 1..128 safe correlation characters")
    source = payload.get("source")
    if not isinstance(source, str) or not _SOURCE.fullmatch(source):
        _invalid("source must use 1..64 vendor-neutral safe characters")
    requested_at = parse_timestamp(payload.get("requested_at"))
    if requested_at is None:
        _invalid("requested_at must be a timezone-aware ISO 8601 timestamp")
    now_utc = _aware_utc(now)
    if requested_at > now_utc:
        _invalid("requested_at must not be in the future")
    if now_utc - requested_at > MAX_EXTERNAL_TRIGGER_AGE:
        _error(
            ExternalControlErrorReason.STALE_EXTERNAL_TRIGGER,
            "requested_at exceeds the inclusive 24-hour replay guard",
        )

    validate_backfill_admission(report_day, now_utc)
    return ExternalRecoveryTrigger(
        target_report_date=report_day,
        mode=DispatchMode.RECOVERY,
        request_id=request_id,
        requested_at=requested_at,
        source=source,
    )


def dispatch_plan_for(trigger: ExternalRecoveryTrigger) -> ExternalDispatchPlan:
    return ExternalDispatchPlan(
        workflow=WRITER_WORKFLOW,
        ref=WRITER_REF,
        target_report_date=trigger.target_report_date,
        mode=DispatchMode.RECOVERY,
        request_id=trigger.request_id,
        trigger_source=trigger.source,
    )


def _report_date(value: Any) -> date:
    if not isinstance(value, str) or len(value) != 10:
        _invalid("target_report_date is required in YYYY-MM-DD format")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        _invalid("target_report_date is required in YYYY-MM-DD format")
    if parsed.isoformat() != value:
        _invalid("target_report_date is required in YYYY-MM-DD format")
    return parsed


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid("validation clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _invalid(detail: str) -> None:
    _error(ExternalControlErrorReason.INVALID_EXTERNAL_TRIGGER, detail)


def _scope_error(detail: str) -> None:
    _error(ExternalControlErrorReason.CONTROL_SCOPE_VIOLATION, detail)


def _error(reason: ExternalControlErrorReason, detail: str) -> None:
    raise ExternalControlError(reason, detail)

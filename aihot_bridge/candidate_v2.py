from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any

from .logical_date import BEIJING_TIMEZONE, report_window_for_date
from .timefields import parse_timestamp


SCHEMA_VERSION = "aihot-bridge/v2"
PRODUCER_CONTRACT_VERSION = "aihot-v2-candidate/1"
SOURCE_RANGE_CONTRACT_VERSION = "aihot-v2-published-desc-range/1"
PRIMARY_CHANNELS = ("selected", "all", "paper")
SUPPLEMENTARY_CHANNELS = ("hot_topics", "daily")
ALL_CHANNELS = PRIMARY_CHANNELS + SUPPLEMENTARY_CHANNELS
MAX_BACKFILL_DAYS = 4
RETENTION_GUARD = timedelta(days=6)


def primary_query_contract() -> dict[str, dict[str, Any]]:
    """Return the business-relevant primary query identity."""
    return {
        "selected": {"mode": "selected", "category": None, "limit": 100},
        "all": {"mode": "all", "category": None, "limit": 100},
        "paper": {"mode": "all", "category": "paper", "limit": 100},
    }


class SourceRangeState(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class SourceRangeProofBasis(str, Enum):
    CROSSED_REPORT_START = "CROSSED_REPORT_START"
    EXHAUSTED = "EXHAUSTED"
    NONE = "NONE"


class CandidateCompletenessState(str, Enum):
    VALID_COMPLETE = "VALID_COMPLETE"
    INVALID = "INVALID"


class CandidateV2ErrorReason(str, Enum):
    REPORT_WINDOW_NOT_CLOSED = "REPORT_WINDOW_NOT_CLOSED"
    BACKFILL_HORIZON_EXCEEDED = "BACKFILL_HORIZON_EXCEEDED"
    SOURCE_RANGE_INCOMPLETE = "SOURCE_RANGE_INCOMPLETE"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    TRUST_INVALID = "TRUST_INVALID"


class CandidateV2Error(ValueError):
    """A stable, machine-readable V2 construction or validation failure."""

    def __init__(self, reason: CandidateV2ErrorReason, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}")


@dataclass(frozen=True)
class SourceRangeEvidence:
    state: SourceRangeState
    proof_basis: SourceRangeProofBasis
    pages_fetched: int
    oldest_published_at: datetime | None
    cursor_exhausted: bool
    ordering_verified: bool
    query_verified: bool
    page_metadata_verified: bool
    invalid_published_at_items: int
    max_pages_reached: bool
    repeated_cursor: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "proof_basis": self.proof_basis.value,
            "pages_fetched": self.pages_fetched,
            "oldest_published_at": (
                format_timestamp(self.oldest_published_at)
                if self.oldest_published_at is not None
                else None
            ),
            "cursor_exhausted": self.cursor_exhausted,
            "ordering_verified": self.ordering_verified,
            "query_verified": self.query_verified,
            "page_metadata_verified": self.page_metadata_verified,
            "invalid_published_at_items": self.invalid_published_at_items,
            "max_pages_reached": self.max_pages_reached,
            "repeated_cursor": self.repeated_cursor,
        }


@dataclass(frozen=True)
class CandidateV2Metadata:
    target_report_date: date
    report_start: datetime
    report_end: datetime
    retrieval_as_of: datetime
    generated_at: datetime
    raw_items: int
    deduplicated_items: int


@dataclass(frozen=True)
class CandidateEvaluation:
    state: CandidateCompletenessState
    reason: CandidateV2ErrorReason | None
    detail: str | None
    metadata: CandidateV2Metadata | None


def validate_backfill_admission(
    target_report_date: date,
    retrieval_as_of: datetime,
) -> None:
    """Authorize an attempted retrieval; this does not prove source completeness."""
    as_of = _aware_utc(retrieval_as_of, "retrieval_as_of")
    report_start, report_end = report_window_for_date(target_report_date)
    report_start_utc = report_start.astimezone(timezone.utc)
    report_end_utc = report_end.astimezone(timezone.utc)
    if as_of < report_end_utc:
        raise CandidateV2Error(
            CandidateV2ErrorReason.REPORT_WINDOW_NOT_CLOSED,
            f"retrieval_as_of={format_timestamp(as_of)} is before "
            f"report_end={format_timestamp(report_end_utc)}",
        )

    if report_start_utc < as_of - RETENTION_GUARD:
        raise CandidateV2Error(
            CandidateV2ErrorReason.BACKFILL_HORIZON_EXCEEDED,
            "report_start is older than the six-day retrieval admission guard",
        )
    latest_closed = latest_closed_report_date(as_of)
    backfill_days = (latest_closed - target_report_date).days
    if backfill_days < 0 or backfill_days > MAX_BACKFILL_DAYS:
        raise CandidateV2Error(
            CandidateV2ErrorReason.BACKFILL_HORIZON_EXCEEDED,
            f"target_report_date is {backfill_days} days behind the latest closed "
            f"report date; allowed range is 0..{MAX_BACKFILL_DAYS}",
        )


def latest_closed_report_date(retrieval_as_of: datetime) -> date:
    as_of = _aware_utc(retrieval_as_of, "retrieval_as_of")
    local_as_of = as_of.astimezone(BEIJING_TIMEZONE)
    latest_closed = local_as_of.date()
    local_noon = datetime.combine(
        latest_closed,
        time(hour=12),
        tzinfo=BEIJING_TIMEZONE,
    )
    if local_as_of < local_noon:
        latest_closed -= timedelta(days=1)
    return latest_closed


def make_source_range_evidence(
    *,
    report_start: datetime | None,
    applicable: bool,
    proof_basis: SourceRangeProofBasis = SourceRangeProofBasis.NONE,
    pages_fetched: int = 0,
    oldest_published_at: datetime | None = None,
    cursor_exhausted: bool = False,
    ordering_verified: bool = False,
    query_verified: bool = False,
    page_metadata_verified: bool = False,
    invalid_published_at_items: int = 0,
    max_pages_reached: bool = False,
    repeated_cursor: bool = False,
) -> SourceRangeEvidence:
    evidence = SourceRangeEvidence(
        state=SourceRangeState.NOT_APPLICABLE,
        proof_basis=proof_basis,
        pages_fetched=pages_fetched,
        oldest_published_at=oldest_published_at,
        cursor_exhausted=cursor_exhausted,
        ordering_verified=ordering_verified,
        query_verified=query_verified,
        page_metadata_verified=page_metadata_verified,
        invalid_published_at_items=invalid_published_at_items,
        max_pages_reached=max_pages_reached,
        repeated_cursor=repeated_cursor,
    )
    state = derive_source_range_state(
        evidence,
        report_start=report_start,
        applicable=applicable,
    )
    return replace(evidence, state=state)


def derive_source_range_state(
    evidence: SourceRangeEvidence,
    *,
    report_start: datetime | None,
    applicable: bool,
) -> SourceRangeState:
    if not applicable:
        return SourceRangeState.NOT_APPLICABLE
    if report_start is None:
        return SourceRangeState.INCOMPLETE
    start = _aware_utc(report_start, "report_start")
    proof_valid = False
    if evidence.proof_basis is SourceRangeProofBasis.EXHAUSTED:
        proof_valid = evidence.cursor_exhausted
    elif evidence.proof_basis is SourceRangeProofBasis.CROSSED_REPORT_START:
        oldest = evidence.oldest_published_at
        proof_valid = oldest is not None and _aware_utc(
            oldest, "oldest_published_at"
        ) < start
    complete = (
        evidence.pages_fetched >= 1
        and proof_valid
        and evidence.ordering_verified
        and evidence.query_verified
        and evidence.page_metadata_verified
        and evidence.invalid_published_at_items == 0
        and not evidence.max_pages_reached
        and not evidence.repeated_cursor
    )
    return SourceRangeState.COMPLETE if complete else SourceRangeState.INCOMPLETE


def evaluate_candidate_completeness(payload: Any) -> CandidateEvaluation:
    try:
        metadata = _validate_candidate_payload(payload)
    except CandidateV2Error as exc:
        return CandidateEvaluation(
            CandidateCompletenessState.INVALID,
            exc.reason,
            exc.detail,
            None,
        )
    return CandidateEvaluation(
        CandidateCompletenessState.VALID_COMPLETE,
        None,
        None,
        metadata,
    )


def validate_candidate_v2(payload: Any) -> CandidateV2Metadata:
    evaluation = evaluate_candidate_completeness(payload)
    if evaluation.state is not CandidateCompletenessState.VALID_COMPLETE:
        assert evaluation.reason is not None and evaluation.detail is not None
        raise CandidateV2Error(evaluation.reason, evaluation.detail)
    assert evaluation.metadata is not None
    return evaluation.metadata


def validated_candidate_bytes(payload: Any) -> bytes:
    validate_candidate_v2(payload)
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _validate_candidate_payload(payload: Any) -> CandidateV2Metadata:
    root = _require_dict(payload, "payload")
    if root.get("schema_version") != SCHEMA_VERSION:
        _schema_error(f"schema_version must be {SCHEMA_VERSION!r}")
    if root.get("producer_contract_version") != PRODUCER_CONTRACT_VERSION:
        _schema_error(
            f"producer_contract_version must be {PRODUCER_CONTRACT_VERSION!r}"
        )

    report_day = _parse_report_date(root.get("target_report_date"))
    report_start, report_end = report_window_for_date(report_day)
    report_start_utc = report_start.astimezone(timezone.utc)
    report_end_utc = report_end.astimezone(timezone.utc)

    report_window = _require_dict(root.get("report_window"), "report_window")
    stored_start = _require_timestamp(report_window.get("from"), "report_window.from")
    stored_end = _require_timestamp(report_window.get("to"), "report_window.to")
    if stored_start != report_start_utc or stored_end != report_end_utc:
        _schema_error("report_window does not match target_report_date")
    if report_window.get("timezone") != "Asia/Shanghai":
        _schema_error("report_window.timezone must be Asia/Shanghai")
    if report_window.get("bounds") != "[from,to)":
        _schema_error("report_window.bounds must be [from,to)")

    retrieval = _require_dict(root.get("retrieval"), "retrieval")
    retrieval_as_of = _require_timestamp(retrieval.get("as_of"), "retrieval.as_of")
    if retrieval.get("upstream_window") != "7d":
        _schema_error("retrieval.upstream_window must be 7d")
    if retrieval.get("by") != "published":
        _schema_error("retrieval.by must be published")
    if retrieval.get("ordering") != "publishedAtDesc":
        _schema_error("retrieval.ordering must be publishedAtDesc")
    if (
        retrieval.get("source_range_contract_version")
        != SOURCE_RANGE_CONTRACT_VERSION
    ):
        _schema_error(
            "retrieval.source_range_contract_version must match the V2 evaluator"
        )
    if retrieval.get("primary_queries") != primary_query_contract():
        _schema_error("retrieval.primary_queries does not match the V2 contract")
    if retrieval_as_of < report_end_utc:
        raise CandidateV2Error(
            CandidateV2ErrorReason.REPORT_WINDOW_NOT_CLOSED,
            "retrieval.as_of is before report_window.to",
        )

    generated_at = _require_timestamp(root.get("generated_at"), "generated_at")
    if generated_at < retrieval_as_of:
        raise CandidateV2Error(
            CandidateV2ErrorReason.TRUST_INVALID,
            "generated_at must be greater than or equal to retrieval.as_of",
        )

    coverage = _require_dict(root.get("coverage"), "coverage")
    primary_raw_items = 0
    for channel in ALL_CHANNELS:
        entry = _require_dict(coverage.get(channel), f"coverage.{channel}")
        item_count = _require_nonnegative_int(
            entry.get("items"), f"coverage.{channel}.items"
        )
        evidence = _source_range_from_payload(
            entry.get("source_range"),
            field=f"coverage.{channel}.source_range",
        )
        applicable = channel in PRIMARY_CHANNELS
        derived = derive_source_range_state(
            evidence,
            report_start=report_start_utc if applicable else None,
            applicable=applicable,
        )
        if evidence.state is not derived:
            _schema_error(f"coverage.{channel}.source_range.state is inconsistent")
        if applicable:
            primary_raw_items += item_count
            if derived is not SourceRangeState.COMPLETE:
                raise CandidateV2Error(
                    CandidateV2ErrorReason.SOURCE_RANGE_INCOMPLETE,
                    f"{channel} source range is not complete",
                )
            if entry.get("status") != "ok" or entry.get("source") != "api":
                raise CandidateV2Error(
                    CandidateV2ErrorReason.TRUST_INVALID,
                    f"{channel} complete range must come from a successful API response",
                )

    summary = _require_dict(root.get("summary"), "summary")
    raw_items = _require_nonnegative_int(summary.get("raw_items"), "summary.raw_items")
    deduplicated_items = _require_nonnegative_int(
        summary.get("deduplicated_items"), "summary.deduplicated_items"
    )
    items = root.get("items")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        _schema_error("items must be a list of objects")
    if raw_items != primary_raw_items:
        _schema_error("summary.raw_items must equal primary coverage item counts")
    if deduplicated_items != len(items) or deduplicated_items > raw_items:
        _schema_error("summary item counts do not match items")

    for index, item in enumerate(items):
        published_at = parse_timestamp(item.get("published_at"))
        if published_at is None:
            raise CandidateV2Error(
                CandidateV2ErrorReason.TRUST_INVALID,
                f"items[{index}].published_at must be timezone-aware",
            )
        if not report_start_utc <= published_at < report_end_utc:
            raise CandidateV2Error(
                CandidateV2ErrorReason.TRUST_INVALID,
                f"items[{index}] is outside the fixed report window",
            )
        source_channels = item.get("source_channels")
        if (
            not isinstance(source_channels, list)
            or not source_channels
            or any(channel not in PRIMARY_CHANNELS for channel in source_channels)
        ):
            raise CandidateV2Error(
                CandidateV2ErrorReason.TRUST_INVALID,
                f"items[{index}].source_channels must contain only primary channels",
            )

    return CandidateV2Metadata(
        target_report_date=report_day,
        report_start=report_start_utc,
        report_end=report_end_utc,
        retrieval_as_of=retrieval_as_of,
        generated_at=generated_at,
        raw_items=raw_items,
        deduplicated_items=deduplicated_items,
    )


def _source_range_from_payload(value: Any, *, field: str) -> SourceRangeEvidence:
    data = _require_dict(value, field)
    try:
        state = SourceRangeState(data.get("state"))
        proof_basis = SourceRangeProofBasis(data.get("proof_basis"))
    except (TypeError, ValueError):
        _schema_error(f"{field} has an invalid state or proof_basis")
    oldest_value = data.get("oldest_published_at")
    oldest = (
        None
        if oldest_value is None
        else _require_timestamp(oldest_value, f"{field}.oldest_published_at")
    )
    return SourceRangeEvidence(
        state=state,
        proof_basis=proof_basis,
        pages_fetched=_require_nonnegative_int(
            data.get("pages_fetched"), f"{field}.pages_fetched"
        ),
        oldest_published_at=oldest,
        cursor_exhausted=_require_bool(
            data.get("cursor_exhausted"), f"{field}.cursor_exhausted"
        ),
        ordering_verified=_require_bool(
            data.get("ordering_verified"), f"{field}.ordering_verified"
        ),
        query_verified=_require_bool(
            data.get("query_verified"), f"{field}.query_verified"
        ),
        page_metadata_verified=_require_bool(
            data.get("page_metadata_verified"),
            f"{field}.page_metadata_verified",
        ),
        invalid_published_at_items=_require_nonnegative_int(
            data.get("invalid_published_at_items"),
            f"{field}.invalid_published_at_items",
        ),
        max_pages_reached=_require_bool(
            data.get("max_pages_reached"), f"{field}.max_pages_reached"
        ),
        repeated_cursor=_require_bool(
            data.get("repeated_cursor"), f"{field}.repeated_cursor"
        ),
    )


def format_timestamp(value: datetime) -> str:
    return _aware_utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _parse_report_date(value: Any) -> date:
    if not isinstance(value, str) or len(value) != 10:
        _schema_error("target_report_date must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        _schema_error("target_report_date must be a valid date")
    if parsed.isoformat() != value:
        _schema_error("target_report_date must use YYYY-MM-DD")
    return parsed


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CandidateV2Error(
            CandidateV2ErrorReason.TRUST_INVALID,
            f"{field} must be a timezone-aware datetime",
        )
    return value.astimezone(timezone.utc)


def _require_timestamp(value: Any, field: str) -> datetime:
    parsed = parse_timestamp(value)
    if parsed is None:
        _schema_error(f"{field} must be a timezone-aware ISO 8601 timestamp")
    return parsed


def _require_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _schema_error(f"{field} must be an object")
    return value


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        _schema_error(f"{field} must be a boolean")
    return value


def _require_nonnegative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _schema_error(f"{field} must be a non-negative integer")
    return value


def _schema_error(detail: str) -> None:
    raise CandidateV2Error(CandidateV2ErrorReason.SCHEMA_INVALID, detail)

from __future__ import annotations

import asyncio
import json
import time as time_module
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from .candidate_v2 import (
    PRIMARY_CHANNELS,
    PRODUCER_CONTRACT_VERSION,
    SOURCE_RANGE_CONTRACT_VERSION,
    CandidateV2Error,
    CandidateV2ErrorReason,
    SourceRangeEvidence,
    SourceRangeProofBasis,
    SourceRangeState,
    format_timestamp,
    latest_closed_report_date,
    make_source_range_evidence,
    primary_query_contract,
    validate_backfill_admission,
)
from .config import Settings
from .logical_date import report_window_for_date
from .normalize import (
    deduplicate,
    normalize_api_item,
    normalize_daily,
    normalize_hot_topic,
    parse_rss_items,
)
from .timefields import parse_timestamp, published_at_in_window
from .upstream import UpstreamClient, UpstreamError


Clock = Callable[[], datetime]


@dataclass(frozen=True)
class ChannelResult:
    channel: str
    coverage: dict[str, Any]
    items: tuple[dict[str, Any], ...]
    logical_requests: int


@dataclass(frozen=True)
class CandidateBuildResult:
    payload: dict[str, Any]
    logical_requests: int
    duration_seconds: float


class V2CandidateService:
    """Build target-anchored V2 candidates without publishing them."""

    def __init__(
        self,
        upstream: UpstreamClient,
        settings: Settings,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._upstream = upstream
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def candidate_for(self, target_report_date: date) -> CandidateBuildResult:
        started = time_module.perf_counter()
        admission_as_of = _aware_utc(self._clock(), "retrieval_as_of")
        validate_backfill_admission(target_report_date, admission_as_of)
        report_start, report_end = report_window_for_date(target_report_date)
        report_start_utc = report_start.astimezone(timezone.utc)
        report_end_utc = report_end.astimezone(timezone.utc)
        historical_target = target_report_date != latest_closed_report_date(
            admission_as_of
        )

        results = await asyncio.gather(
            self._primary_channel(
                "selected",
                {"mode": "selected", "window": "7d", "by": "published", "limit": 100},
                "/feed.xml",
                report_start_utc,
                report_end_utc,
            ),
            self._primary_channel(
                "all",
                {"mode": "all", "window": "7d", "by": "published", "limit": 100},
                "/feed/all.xml",
                report_start_utc,
                report_end_utc,
            ),
            self._primary_channel(
                "paper",
                {
                    "mode": "all",
                    "category": "paper",
                    "window": "7d",
                    "by": "published",
                    "limit": 100,
                },
                "/feed/category/paper.xml",
                report_start_utc,
                report_end_utc,
            ),
            self._hot_topics_channel(historical_target=historical_target),
            self._daily_channel(
                target_report_date,
                historical_target=historical_target,
                report_start=report_start_utc,
                report_end=report_end_utc,
            ),
        )

        retrieval_as_of = max(
            admission_as_of,
            _aware_utc(self._clock(), "retrieval_as_of"),
        )
        validate_backfill_admission(target_report_date, retrieval_as_of)
        generated_at = _aware_utc(self._clock(), "generated_at")
        if generated_at < retrieval_as_of:
            raise CandidateV2Error(
                CandidateV2ErrorReason.TRUST_INVALID,
                "generated_at clock value is before retrieval_as_of",
            )

        coverage = {result.channel: result.coverage for result in results}
        primary_items = [
            deepcopy(item)
            for result in results
            if result.channel in PRIMARY_CHANNELS
            for item in result.items
        ]
        items = _deterministic_deduplicate(primary_items)
        payload = {
            "schema_version": "aihot-bridge/v2",
            "producer_contract_version": PRODUCER_CONTRACT_VERSION,
            "target_report_date": target_report_date.isoformat(),
            "generated_at": format_timestamp(generated_at),
            "report_window": {
                "from": format_timestamp(report_start_utc),
                "to": format_timestamp(report_end_utc),
                "timezone": "Asia/Shanghai",
                "bounds": "[from,to)",
            },
            "retrieval": {
                "as_of": format_timestamp(retrieval_as_of),
                "upstream_window": "7d",
                "by": "published",
                "ordering": "publishedAtDesc",
                "source_range_contract_version": SOURCE_RANGE_CONTRACT_VERSION,
                "primary_queries": primary_query_contract(),
            },
            "coverage": coverage,
            "summary": {
                "raw_items": len(primary_items),
                "deduplicated_items": len(items),
            },
            "items": items,
        }
        return CandidateBuildResult(
            payload=payload,
            logical_requests=sum(result.logical_requests for result in results),
            duration_seconds=time_module.perf_counter() - started,
        )

    async def _primary_channel(
        self,
        channel: str,
        params: dict[str, Any],
        rss_path: str,
        report_start: datetime,
        report_end: datetime,
    ) -> ChannelResult:
        try:
            raw_items, evidence, requests = await self._paginate_primary(
                params,
                report_start=report_start,
            )
            normalized = [normalize_api_item(item, channel) for item in raw_items]
            items = tuple(
                item
                for item in normalized
                if published_at_in_window(item, report_start, report_end)
            )
            complete = evidence.state is SourceRangeState.COMPLETE
            return ChannelResult(
                channel,
                _coverage(
                    "ok" if complete else "partial",
                    "api",
                    len(items),
                    evidence,
                    error=None if complete else "SOURCE_RANGE_INCOMPLETE",
                ),
                items,
                requests,
            )
        except UpstreamError as api_exc:
            api_error = str(api_exc)

        evidence = make_source_range_evidence(
            report_start=report_start,
            applicable=True,
        )
        try:
            rss = await self._upstream.get_text(rss_path)
            items = tuple(
                parse_rss_items(
                    rss,
                    channel,
                    window_start=report_start,
                    window_end=report_end,
                )
            )
            return ChannelResult(
                channel,
                _coverage(
                    "fallback",
                    "rss",
                    len(items),
                    evidence,
                    error="SOURCE_RANGE_INCOMPLETE",
                    api_error=api_error,
                ),
                items,
                2,
            )
        except (UpstreamError, ValueError) as rss_exc:
            return ChannelResult(
                channel,
                _coverage(
                    "failed",
                    None,
                    0,
                    evidence,
                    error="SOURCE_RANGE_INCOMPLETE",
                    api_error=api_error,
                    rss_error=str(rss_exc),
                ),
                (),
                2,
            )

    async def _paginate_primary(
        self,
        params: dict[str, Any],
        *,
        report_start: datetime,
    ) -> tuple[list[dict[str, Any]], SourceRangeEvidence, int]:
        output: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        pages_fetched = 0
        oldest: datetime | None = None
        previous: datetime | None = None
        invalid_timestamps = 0
        ordering_verified = True
        query_verified = True
        page_metadata_verified = True
        cursor_exhausted = False
        max_pages_reached = False
        repeated_cursor = False
        proof_basis = SourceRangeProofBasis.NONE

        for page_number in range(1, self._settings.v2_max_pages + 1):
            request_params = dict(params)
            if cursor is not None:
                request_params["cursor"] = cursor
            payload = await self._upstream.get_json(
                "/api/v1/items",
                params=request_params,
            )
            pages_fetched += 1
            page_items = payload.get("items")
            page = payload.get("page")
            if not isinstance(page_items, list) or any(
                not isinstance(item, dict) for item in page_items
            ):
                raise UpstreamError("unexpected items schema")
            if not isinstance(page, dict):
                raise UpstreamError("unexpected items.page schema")
            output.extend(page_items)

            query_verified = query_verified and _query_matches(payload, params)
            page_metadata_verified = page_metadata_verified and _page_matches(
                page,
                len(page_items),
            )

            page_oldest: datetime | None = None
            for item in page_items:
                published = parse_timestamp(item.get("publishedAt"))
                if published is None:
                    invalid_timestamps += 1
                    continue
                if previous is not None and published > previous:
                    ordering_verified = False
                previous = published
                page_oldest = published if page_oldest is None else min(page_oldest, published)
                oldest = published if oldest is None else min(oldest, published)

            has_more = page.get("hasMore")
            next_cursor = page.get("nextCursor")
            if has_more is False:
                cursor_exhausted = True
                proof_basis = SourceRangeProofBasis.EXHAUSTED
                break
            if has_more is not True or not isinstance(next_cursor, str) or not next_cursor:
                page_metadata_verified = False
                break
            if page_oldest is not None and page_oldest < report_start:
                proof_basis = SourceRangeProofBasis.CROSSED_REPORT_START
                break
            if next_cursor in seen_cursors:
                repeated_cursor = True
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            if page_number == self._settings.v2_max_pages:
                max_pages_reached = True
                break

        evidence = make_source_range_evidence(
            report_start=report_start,
            applicable=True,
            proof_basis=proof_basis,
            pages_fetched=pages_fetched,
            oldest_published_at=oldest,
            cursor_exhausted=cursor_exhausted,
            ordering_verified=ordering_verified,
            query_verified=query_verified,
            page_metadata_verified=page_metadata_verified,
            invalid_published_at_items=invalid_timestamps,
            max_pages_reached=max_pages_reached,
            repeated_cursor=repeated_cursor,
        )
        return output, evidence, pages_fetched

    async def _hot_topics_channel(self, *, historical_target: bool) -> ChannelResult:
        evidence = _not_applicable_evidence()
        if historical_target:
            return ChannelResult(
                "hot_topics",
                _coverage("unavailable_for_target", None, 0, evidence),
                (),
                0,
            )
        try:
            payload = await self._upstream.get_json("/api/v1/hot-topics")
            raw_items = payload.get("items")
            if payload.get("schemaVersion") != 1 or not isinstance(raw_items, list) or any(
                not isinstance(item, dict) for item in raw_items
            ):
                raise UpstreamError("unexpected hot_topics schema")
            items = [normalize_hot_topic(item) for item in raw_items]
            return ChannelResult(
                "hot_topics",
                _coverage("ok", "api", len(items), evidence),
                (),
                1,
            )
        except UpstreamError as exc:
            return ChannelResult(
                "hot_topics",
                _coverage("failed", None, 0, evidence, error=str(exc)),
                (),
                1,
            )

    async def _daily_channel(
        self,
        target_report_date: date,
        *,
        historical_target: bool,
        report_start: datetime,
        report_end: datetime,
    ) -> ChannelResult:
        evidence = _not_applicable_evidence()
        if historical_target:
            return ChannelResult(
                "daily",
                _coverage("unavailable_for_target", None, 0, evidence),
                (),
                0,
            )
        try:
            payload = await self._upstream.get_json("/api/v1/dailies/latest")
            report = payload.get("report")
            if payload.get("schemaVersion") != 1 or not isinstance(report, dict):
                raise UpstreamError("unexpected daily.report schema")
            if report.get("date") != target_report_date.isoformat():
                return ChannelResult(
                    "daily",
                    _coverage(
                        "unavailable_for_target",
                        "api",
                        0,
                        evidence,
                        error="latest daily does not match target_report_date",
                    ),
                    (),
                    1,
                )
            normalize_daily(report)
            return ChannelResult(
                "daily",
                _coverage("ok", "api", 1, evidence),
                (),
                1,
            )
        except UpstreamError as api_exc:
            api_error = str(api_exc)
        try:
            rss = await self._upstream.get_text("/feed/daily.xml")
            items = parse_rss_items(
                rss,
                "daily",
                window_start=report_start,
                window_end=report_end,
            )
            return ChannelResult(
                "daily",
                _coverage("fallback", "rss", len(items), evidence, api_error=api_error),
                (),
                2,
            )
        except (UpstreamError, ValueError) as rss_exc:
            return ChannelResult(
                "daily",
                _coverage(
                    "failed",
                    None,
                    0,
                    evidence,
                    error="API and RSS unavailable",
                    api_error=api_error,
                    rss_error=str(rss_exc),
                ),
                (),
                2,
            )


def _query_matches(payload: dict[str, Any], params: dict[str, Any]) -> bool:
    query = payload.get("query")
    if payload.get("schemaVersion") != 1 or not isinstance(query, dict):
        return False
    return (
        query.get("mode") == params.get("mode")
        and query.get("category") == params.get("category")
        and query.get("window") == "7d"
        and query.get("by") == "published"
        and query.get("ordering") == "publishedAtDesc"
    )


def _page_matches(page: dict[str, Any], item_count: int) -> bool:
    count = page.get("count")
    has_more = page.get("hasMore")
    next_cursor = page.get("nextCursor")
    if not isinstance(count, int) or isinstance(count, bool) or count != item_count:
        return False
    if not isinstance(has_more, bool):
        return False
    if has_more:
        return isinstance(next_cursor, str) and bool(next_cursor)
    return next_cursor is None


def _coverage(
    status: str,
    source: str | None,
    items: int,
    evidence: SourceRangeEvidence,
    *,
    error: str | None = None,
    api_error: str | None = None,
    rss_error: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "source": source,
        "items": items,
        "source_range": evidence.to_payload(),
        **({"error": error} if error is not None else {}),
        **({"api_error": api_error} if api_error is not None else {}),
        **({"rss_error": rss_error} if rss_error is not None else {}),
    }


def _not_applicable_evidence() -> SourceRangeEvidence:
    return make_source_range_evidence(report_start=None, applicable=False)


def _deterministic_deduplicate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted((deepcopy(item) for item in items), key=_canonical_item)
    output = deduplicate(ordered)
    for item in output:
        channels = item.get("source_channels")
        if isinstance(channels, list):
            item["source_channels"] = sorted(set(channels))
    return sorted(output, key=_canonical_item)


def _canonical_item(item: dict[str, Any]) -> str:
    return json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CandidateV2Error(
            CandidateV2ErrorReason.TRUST_INVALID,
            f"{field} must be a timezone-aware datetime",
        )
    return value.astimezone(timezone.utc)

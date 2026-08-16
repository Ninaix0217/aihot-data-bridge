from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings
from .normalize import (
    deduplicate,
    normalize_api_item,
    normalize_daily,
    normalize_hot_topic,
    parse_rss_items,
)
from .upstream import UpstreamClient, UpstreamError


Clock = Callable[[], datetime]


class BridgeService:
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

    async def today(self) -> dict[str, Any]:
        window_end = _utc(self._clock())
        window_start = window_end - timedelta(hours=24)

        results = await asyncio.gather(
            self._items_channel("selected", {"mode": "selected", "window": "24h", "limit": 100}, "/feed.xml", window_start, window_end),
            self._items_channel("all", {"mode": "all", "window": "24h", "limit": 100}, "/feed/all.xml", window_start, window_end),
            self._items_channel("paper", {"mode": "all", "window": "24h", "category": "paper", "limit": 100}, "/feed/category/paper.xml", window_start, window_end),
            self._hot_topics_channel(),
            self._daily_channel(window_start, window_end),
        )

        coverage: dict[str, dict[str, Any]] = {}
        raw_items: list[dict[str, Any]] = []
        for channel, channel_coverage, channel_items in results:
            coverage[channel] = channel_coverage
            raw_items.extend(channel_items)

        items = deduplicate(raw_items)
        generated_at = _utc(self._clock())
        return {
            "schema_version": "aihot-bridge/v1",
            "generated_at": _format_time(generated_at),
            "window": {
                "type": "rolling_24h",
                "from": _format_time(window_start),
                "to": _format_time(window_end),
            },
            "coverage": coverage,
            "summary": {
                "raw_items": len(raw_items),
                "deduplicated_items": len(items),
            },
            "items": items,
        }

    async def _items_channel(
        self,
        channel: str,
        params: dict[str, Any],
        rss_path: str,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        try:
            raw_items, partial_reason = await self._paginated_items(params)
            items = [normalize_api_item(item, channel) for item in raw_items]
            if partial_reason:
                return channel, _coverage("partial", "api", len(items), error=partial_reason), items
            return channel, _coverage("ok", "api", len(items)), items
        except UpstreamError as api_exc:
            api_error = str(api_exc)

        try:
            rss = await self._upstream.get_text(rss_path)
            items = parse_rss_items(
                rss,
                channel,
                window_start=window_start,
                window_end=window_end,
            )
            return channel, _coverage("fallback", "rss", len(items), api_error=api_error), items
        except (UpstreamError, ValueError) as rss_exc:
            rss_error = str(rss_exc)
            return (
                channel,
                _coverage(
                    "failed",
                    None,
                    0,
                    error="API and RSS unavailable",
                    api_error=api_error,
                    rss_error=rss_error,
                ),
                [],
            )

    async def _hot_topics_channel(
        self,
    ) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        try:
            payload = await self._upstream.get_json("/api/v1/hot-topics")
            raw_items = _list_of_dicts(payload.get("items"), "hot_topics.items")
            items = [normalize_hot_topic(item) for item in raw_items]
            return "hot_topics", _coverage("ok", "api", len(items)), items
        except UpstreamError as exc:
            return "hot_topics", _coverage("failed", None, 0, error=str(exc)), []

    async def _daily_channel(
        self, window_start: datetime, window_end: datetime
    ) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        try:
            payload = await self._upstream.get_json("/api/v1/dailies/latest")
            report = payload.get("report")
            if not isinstance(report, dict):
                raise UpstreamError("unexpected daily.report schema")
            items = [normalize_daily(report)]
            return "daily", _coverage("ok", "api", len(items)), items
        except UpstreamError as api_exc:
            api_error = str(api_exc)

        try:
            rss = await self._upstream.get_text("/feed/daily.xml")
            items = parse_rss_items(
                rss,
                "daily",
                window_start=window_start,
                window_end=window_end,
            )
            return "daily", _coverage("fallback", "rss", len(items), api_error=api_error), items
        except (UpstreamError, ValueError) as rss_exc:
            return (
                "daily",
                _coverage(
                    "failed",
                    None,
                    0,
                    error="API and RSS unavailable",
                    api_error=api_error,
                    rss_error=str(rss_exc),
                ),
                [],
            )

    async def _paginated_items(
        self, base_params: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], str | None]:
        output: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()

        for page_number in range(1, self._settings.max_pages + 1):
            params = dict(base_params)
            if cursor:
                params["cursor"] = cursor
            payload = await self._upstream.get_json("/api/v1/items", params=params)
            output.extend(_list_of_dicts(payload.get("items"), "items"))
            page = payload.get("page")
            if not isinstance(page, dict):
                raise UpstreamError("unexpected items.page schema")
            next_cursor = page.get("nextCursor")
            if not next_cursor:
                return output, None
            if not isinstance(next_cursor, str):
                raise UpstreamError("unexpected nextCursor type")
            if next_cursor in seen_cursors:
                return output, "upstream repeated nextCursor"
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            if page_number == self._settings.max_pages:
                return output, f"maximum page limit reached ({self._settings.max_pages})"

        raise AssertionError("pagination loop exhausted unexpectedly")


def _coverage(
    status: str,
    source: str | None,
    items: int,
    *,
    error: str | None = None,
    api_error: str | None = None,
    rss_error: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "source": source,
        "items": items,
        "error": error,
        **({"api_error": api_error} if api_error is not None else {}),
        **({"rss_error": rss_error} if rss_error is not None else {}),
    }


def _list_of_dicts(value: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise UpstreamError(f"unexpected {field} schema")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")

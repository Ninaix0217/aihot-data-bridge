from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx


logger = logging.getLogger(__name__)


class UpstreamError(RuntimeError):
    """A safe, user-visible description of an upstream failure."""


Sleep = Callable[[float], Awaitable[None]]


class UpstreamClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_retries: int = 2,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._client = client
        self._max_retries = max_retries
        self._sleep = sleep

    async def get_json(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        response = await self._request(path, params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError("malformed JSON") from exc
        if not isinstance(payload, dict):
            raise UpstreamError("unexpected JSON root type")
        return payload

    async def get_text(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> str:
        return (await self._request(path, params=params)).text

    async def _request(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        attempts = self._max_retries + 1
        for attempt in range(attempts):
            try:
                response = await self._client.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt + 1 >= attempts:
                    kind = "timeout" if isinstance(exc, httpx.TimeoutException) else "network error"
                    raise UpstreamError(kind) from exc
                delay = 0.25 * (2**attempt)
                logger.warning("upstream transport failure path=%s retry_in=%.2fs", path, delay)
                await self._sleep(delay)
                continue

            status = response.status_code
            if status == 429:
                if attempt + 1 >= attempts:
                    raise UpstreamError("HTTP 429 after retries")
                delay = _retry_after_seconds(response.headers.get("Retry-After"))
                if delay is None:
                    delay = 0.5 * (2**attempt)
                logger.warning("upstream rate limited path=%s retry_in=%.2fs", path, delay)
                await self._sleep(delay)
                continue

            if 500 <= status <= 599:
                if attempt + 1 >= attempts:
                    raise UpstreamError(f"HTTP {status} after retries")
                delay = 0.5 * (2**attempt)
                logger.warning("upstream server failure path=%s status=%s retry_in=%.2fs", path, status, delay)
                await self._sleep(delay)
                continue

            if status >= 400:
                raise UpstreamError(f"HTTP {status}")

            logger.info("upstream success path=%s status=%s", path, status)
            return response

        raise AssertionError("retry loop exhausted unexpectedly")


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


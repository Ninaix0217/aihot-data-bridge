from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from aihot_bridge.config import Settings
from aihot_bridge.main import create_app
from aihot_bridge.normalize import deduplicate, normalize_api_item
from aihot_bridge.service import BridgeService
from aihot_bridge.upstream import UpstreamClient


NOW = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)


def api_item(
    item_id: str,
    *,
    url: str | None = None,
    published: str = "2026-08-16T12:00:00Z",
    discovered: str = "2026-08-16T12:05:00Z",
    category: str = "industry",
) -> dict:
    return {
        "id": item_id,
        "title": f"Title {item_id}",
        "originalTitle": f"Original {item_id}",
        "summary": f"Summary {item_id}",
        "source": {"name": "Example Source"},
        "links": {
            "aihot": f"https://aihot.virxact.com/items/{item_id}",
            "original": url or f"https://example.com/{item_id}",
        },
        "publishedAt": published,
        "discoveredAt": discovered,
        "category": category,
        "score": 42,
        "selected": False,
        "reason": None,
        "attribution": {"name": "AIHOT"},
    }


def items_payload(items: list[dict], next_cursor: str | None = None) -> dict:
    return {
        "schemaVersion": 1,
        "items": items,
        "page": {
            "count": len(items),
            "hasMore": next_cursor is not None,
            "nextCursor": next_cursor,
        },
    }


def rss(item_id: str = "rss-paper") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>AIHOT</title><item>
<title>RSS paper</title>
<link>https://aihot.virxact.com/items/{item_id}</link>
<description><![CDATA[<p>RSS summary</p><p><a href="https://example.com/{item_id}">阅读原文</a></p>]]></description>
<category>论文</category><pubDate>Sun, 16 Aug 2026 12:00:00 GMT</pubDate>
<guid isPermaLink="false">{item_id}</guid>
<author>noreply@aihot.virxact.com (RSS Source)</author>
</item></channel></rss>"""


def daily_payload() -> dict:
    return {
        "schemaVersion": 1,
        "report": {
            "date": "2026-08-16",
            "generatedAt": "2026-08-16T00:00:08Z",
            "windowStart": "2026-08-15T00:00:00Z",
            "windowEnd": "2026-08-16T00:00:00Z",
            "links": {"aihot": "https://aihot.virxact.com/daily/2026-08-16"},
            "lead": None,
            "sections": [],
            "flashes": [],
        },
    }


def query(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.url.query.decode())


def success_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/v1/items":
        params = query(request)
        if params.get("category") == ["paper"]:
            return httpx.Response(200, json=items_payload([api_item("b", category="paper")]))
        if params.get("mode") == ["selected"]:
            return httpx.Response(200, json=items_payload([api_item("a")]))
        return httpx.Response(200, json=items_payload([api_item("a"), api_item("b", category="paper")]))
    if path == "/api/v1/hot-topics":
        return httpx.Response(
            200,
            json={
                "schemaVersion": 1,
                "count": 1,
                "items": [
                    {
                        "rank": 1,
                        "id": "b",
                        "title": "Title b",
                        "source": {"name": "Example Source"},
                        "links": {
                            "aihot": "https://aihot.virxact.com/items/b",
                            "original": "https://example.com/b",
                            "story": "https://aihot.virxact.com/story/b",
                        },
                        "sourceCount": 2,
                        "signalCount": 2,
                        "sourceNames": ["One", "Two"],
                        "latestAt": "2026-08-16T12:06:00Z",
                    }
                ],
            },
        )
    if path == "/api/v1/dailies/latest":
        return httpx.Response(200, json=daily_payload())
    raise AssertionError(f"unexpected request: {request.url}")


def make_service(handler, *, max_retries: int = 0, max_pages: int = 10):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://aihot.virxact.com",
    )
    upstream = UpstreamClient(client, max_retries=max_retries)
    settings = Settings(max_retries=max_retries, max_pages=max_pages)
    return BridgeService(upstream, settings, clock=lambda: NOW), client


@pytest.mark.asyncio
async def test_all_five_apis_success_merge_and_deduplicate():
    service, client = make_service(success_handler)
    try:
        result = await service.today()
    finally:
        await client.aclose()

    assert {entry["status"] for entry in result["coverage"].values()} == {"ok"}
    assert result["summary"] == {"raw_items": 6, "deduplicated_items": 3}
    item_b = next(item for item in result["items"] if item["id"] == "b")
    assert item_b["source_channels"] == ["all", "paper", "hot_topics"]


@pytest.mark.asyncio
async def test_paper_api_failure_uses_rss_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/items" and query(request).get("category") == ["paper"]:
            return httpx.Response(502)
        if request.url.path == "/feed/category/paper.xml":
            return httpx.Response(200, text=rss())
        return success_handler(request)

    service, client = make_service(handler)
    try:
        result = await service.today()
    finally:
        await client.aclose()

    coverage = result["coverage"]["paper"]
    assert coverage["status"] == "fallback"
    assert coverage["source"] == "rss"
    assert coverage["items"] == 1
    assert coverage["api_error"] == "HTTP 502 after retries"
    paper = next(item for item in result["items"] if item["id"] == "rss-paper")
    assert paper["category"] == "paper"


@pytest.mark.asyncio
async def test_api_and_rss_failure_is_reported_but_endpoint_returns_200():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/items" and query(request).get("category") == ["paper"]:
            return httpx.Response(503)
        if request.url.path == "/feed/category/paper.xml":
            return httpx.Response(503)
        return success_handler(request)

    service, upstream_http = make_service(handler)
    app = create_app(service)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as bridge_http:
                response = await bridge_http.get("/aihot/today")
    finally:
        await upstream_http.aclose()

    assert response.status_code == 200
    assert response.json()["coverage"]["paper"]["status"] == "failed"


@pytest.mark.asyncio
async def test_pagination_uses_next_cursor_and_merges_pages():
    seen_cursors: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = query(request)
        cursor = params.get("cursor", [None])[0]
        seen_cursors.append(cursor)
        if cursor is None:
            return httpx.Response(200, json=items_payload([api_item("page-1")], "next-1"))
        assert cursor == "next-1"
        return httpx.Response(200, json=items_payload([api_item("page-2")]))

    service, client = make_service(handler)
    try:
        items, partial = await service._paginated_items(
            {"mode": "all", "window": "24h", "limit": 100}
        )
    finally:
        await client.aclose()

    assert [item["id"] for item in items] == ["page-1", "page-2"]
    assert partial is None
    assert seen_cursors == [None, "next-1"]


def test_deduplicate_by_stable_id_and_original_url():
    first = normalize_api_item(api_item("same-id", url="https://example.com/one"), "selected")
    same_id = normalize_api_item(api_item("same-id", url="https://example.com/two"), "all")
    same_url = normalize_api_item(
        api_item("different-id", url="https://example.com/one?utm_source=test"), "paper"
    )

    result = deduplicate([first, same_id, same_url])

    assert len(result) == 1
    assert result[0]["source_channels"] == ["selected", "all", "paper"]


def test_published_and_collected_times_remain_distinct():
    normalized = normalize_api_item(
        api_item(
            "time-test",
            published="2026-08-16T01:00:00.000Z",
            discovered="2026-08-16T06:30:00.393Z",
        ),
        "all",
    )

    assert normalized["published_at"] == "2026-08-16T01:00:00.000Z"
    assert normalized["collected_at"] == "2026-08-16T06:30:00.393Z"
    assert normalized["published_at"] != normalized["collected_at"]


@pytest.mark.asyncio
async def test_429_respects_retry_after():
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"ok": True})

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://example.com"
    ) as client:
        upstream = UpstreamClient(client, max_retries=1, sleep=record_sleep)
        assert await upstream.get_json("/test") == {"ok": True}

    assert calls == 2
    assert sleeps == [2.0]


@pytest.mark.asyncio
async def test_5xx_retry_recovers():
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(502)
        return httpx.Response(200, json={"recovered": True})

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://example.com"
    ) as client:
        upstream = UpstreamClient(client, max_retries=1, sleep=record_sleep)
        assert await upstream.get_json("/test") == {"recovered": True}

    assert calls == 2
    assert sleeps == [0.5]


@pytest.mark.asyncio
async def test_max_page_limit_marks_coverage_partial():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json=items_payload([api_item(f"page-{calls}")], f"cursor-{calls}"),
        )

    service, client = make_service(handler, max_pages=2)
    try:
        _, coverage, items = await service._items_channel(
            "all",
            {"mode": "all", "window": "24h", "limit": 100},
            "/feed/all.xml",
            NOW.replace(day=16),
            NOW,
        )
    finally:
        await client.aclose()

    assert calls == 2
    assert len(items) == 2
    assert coverage["status"] == "partial"
    assert coverage["source"] == "api"
    assert coverage["error"] == "maximum page limit reached (2)"


@pytest.mark.asyncio
async def test_health_endpoint():
    service, upstream_http = make_service(success_handler)
    app = create_app(service)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as bridge_http:
                response = await bridge_http.get("/health")
    finally:
        await upstream_http.aclose()

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_only_required_public_routes_are_exposed():
    service, upstream_http = make_service(success_handler)
    app = create_app(service)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as bridge_http:
                responses = {
                    path: await bridge_http.get(path)
                    for path in ("/", "/docs", "/redoc", "/openapi.json")
                }
    finally:
        await upstream_http.aclose()

    assert all(response.status_code == 404 for response in responses.values())
    assert {route.path for route in app.routes} == {"/health", "/aihot/today"}

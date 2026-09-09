from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from aihot_bridge.candidate_v2 import (
    CandidateCompletenessState,
    CandidateV2Error,
    CandidateV2ErrorReason,
    SourceRangeProofBasis,
    SourceRangeState,
    evaluate_candidate_completeness,
)
from aihot_bridge.config import Settings
from aihot_bridge.retrieval_v2 import V2CandidateService
from aihot_bridge.upstream import UpstreamClient


REPORT_DAY = date(2026, 9, 8)
REPORT_START = datetime(2026, 9, 7, 4, tzinfo=timezone.utc)
REPORT_END = datetime(2026, 9, 8, 4, tzinfo=timezone.utc)
ITEM_PARAMS = {"mode": "all", "window": "7d", "by": "published", "limit": 100}


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def request_query(request: httpx.Request) -> dict[str, str]:
    parsed = parse_qs(request.url.query.decode())
    return {key: values[0] for key, values in parsed.items()}


def api_item(
    item_id: str,
    published_at: str | None,
    *,
    discovered_at: str | None = None,
) -> dict:
    item = {
        "id": item_id,
        "category": "AI 模型",
        "title": f"Item {item_id}",
        "summary": f"Summary {item_id}",
        "links": {
            "original": f"https://example.com/{item_id}",
            "aihot": f"https://aihot.virxact.com/items/{item_id}",
        },
        "source": {"name": "Example"},
        "discoveredAt": discovered_at or published_at,
    }
    if published_at is not None:
        item["publishedAt"] = published_at
    return item


def items_response(
    request: httpx.Request,
    items: list[dict],
    next_cursor: str | None,
    *,
    query_overrides: dict | None = None,
    page_overrides: dict | None = None,
) -> httpx.Response:
    params = request_query(request)
    query = {
        "mode": params.get("mode"),
        "category": params.get("category"),
        "window": params.get("window"),
        "q": None,
        "by": params.get("by"),
        "ordering": "publishedAtDesc",
    }
    query.update(query_overrides or {})
    page = {
        "count": len(items),
        "hasMore": next_cursor is not None,
        "nextCursor": next_cursor,
    }
    page.update(page_overrides or {})
    return httpx.Response(
        200,
        json={"schemaVersion": 1, "query": query, "items": items, "page": page},
    )


def rss(item_id: str = "rss-1") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0"><channel><item>
<title>RSS item</title>
<link>https://aihot.virxact.com/items/{item_id}</link>
<guid>{item_id}</guid>
<pubDate>Mon, 07 Sep 2026 05:00:00 GMT</pubDate>
<description><![CDATA[<p>RSS summary</p><a href="https://example.com/{item_id}">source</a>]]></description>
<category>AI 模型</category><author>Example</author>
</item></channel></rss>"""


def make_service(
    handler,
    *,
    clock=lambda: utc("2026-09-08T05:10:00Z"),
    v2_max_pages: int = 30,
) -> tuple[V2CandidateService, httpx.AsyncClient]:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://aihot.virxact.com",
    )
    service = V2CandidateService(
        UpstreamClient(client, max_retries=0),
        Settings(max_retries=0, v2_max_pages=v2_max_pages),
        clock=clock,
    )
    return service, client


def test_v2_page_cap_is_decoupled_from_v1_default():
    settings = Settings()

    assert settings.max_pages == 10
    assert settings.v2_max_pages == 30


@pytest.mark.asyncio
async def test_unclosed_report_window_is_rejected_before_upstream_requests():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("upstream must not be called before report_end")

    service, client = make_service(
        handler,
        clock=lambda: utc("2026-09-08T03:59:59Z"),
    )
    try:
        with pytest.raises(CandidateV2Error) as error:
            await service.candidate_for(REPORT_DAY)
    finally:
        await client.aclose()

    assert error.value.reason is CandidateV2ErrorReason.REPORT_WINDOW_NOT_CLOSED
    assert requests == []


def cursor_handler(pages: dict[str | None, tuple[list[dict], str | None]], **kwargs):
    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request_query(request).get("cursor")
        items, next_cursor = pages[cursor]
        return items_response(request, items, next_cursor, **kwargs)

    return handler


@pytest.mark.asyncio
async def test_pagination_stops_on_third_page_after_strictly_crossing_start():
    pages = {
        None: ([api_item("1", "2026-09-08T03:00:00Z")], "c1"),
        "c1": ([api_item("2", "2026-09-07T05:00:00Z")], "c2"),
        "c2": ([api_item("3", "2026-09-07T03:59:59Z")], "unused"),
    }
    service, client = make_service(cursor_handler(pages))
    try:
        _, evidence, requests = await service._paginate_primary(
            ITEM_PARAMS,
            report_start=REPORT_START,
        )
    finally:
        await client.aclose()

    assert requests == 3
    assert evidence.pages_fetched == 3
    assert evidence.proof_basis is SourceRangeProofBasis.CROSSED_REPORT_START
    assert evidence.state is SourceRangeState.COMPLETE


@pytest.mark.asyncio
async def test_equal_start_with_cursor_continues_until_next_page_crosses():
    seen: list[str | None] = []
    pages = {
        None: ([api_item("1", "2026-09-08T03:00:00Z")], "c1"),
        "c1": ([api_item("2", "2026-09-07T05:00:00Z")], "c2"),
        "c2": ([api_item("3", "2026-09-07T04:00:00Z")], "c3"),
        "c3": ([api_item("4", "2026-09-07T03:59:59Z")], "unused"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request_query(request).get("cursor")
        seen.append(cursor)
        items, next_cursor = pages[cursor]
        return items_response(request, items, next_cursor)

    service, client = make_service(handler)
    try:
        _, evidence, _ = await service._paginate_primary(
            ITEM_PARAMS,
            report_start=REPORT_START,
        )
    finally:
        await client.aclose()

    assert seen == [None, "c1", "c2", "c3"]
    assert evidence.pages_fetched == 4
    assert evidence.proof_basis is SourceRangeProofBasis.CROSSED_REPORT_START
    assert evidence.state is SourceRangeState.COMPLETE


@pytest.mark.asyncio
async def test_equal_start_is_complete_when_cursor_is_exhausted():
    pages = {
        None: ([api_item("1", "2026-09-08T03:00:00Z")], "c1"),
        "c1": ([api_item("2", "2026-09-07T05:00:00Z")], "c2"),
        "c2": ([api_item("3", "2026-09-07T04:00:00Z")], None),
    }
    service, client = make_service(cursor_handler(pages))
    try:
        _, evidence, _ = await service._paginate_primary(
            ITEM_PARAMS,
            report_start=REPORT_START,
        )
    finally:
        await client.aclose()

    assert evidence.pages_fetched == 3
    assert evidence.cursor_exhausted
    assert evidence.proof_basis is SourceRangeProofBasis.EXHAUSTED
    assert evidence.state is SourceRangeState.COMPLETE


@pytest.mark.asyncio
async def test_max_pages_without_range_proof_is_incomplete():
    pages = {
        None: ([api_item("1", "2026-09-08T03:00:00Z")], "c1"),
        "c1": ([api_item("2", "2026-09-07T05:00:00Z")], "c2"),
    }
    service, client = make_service(cursor_handler(pages), v2_max_pages=2)
    try:
        _, evidence, _ = await service._paginate_primary(
            ITEM_PARAMS,
            report_start=REPORT_START,
        )
    finally:
        await client.aclose()

    assert evidence.pages_fetched == 2
    assert evidence.max_pages_reached
    assert evidence.proof_basis is SourceRangeProofBasis.NONE
    assert evidence.state is SourceRangeState.INCOMPLETE


@pytest.mark.asyncio
async def test_proof_on_max_page_is_complete_not_a_cap_failure():
    pages = {
        None: ([api_item("1", "2026-09-08T03:00:00Z")], "c1"),
        "c1": ([api_item("2", "2026-09-07T03:59:59Z")], "unused"),
    }
    service, client = make_service(cursor_handler(pages), v2_max_pages=2)
    try:
        _, evidence, _ = await service._paginate_primary(
            ITEM_PARAMS,
            report_start=REPORT_START,
        )
    finally:
        await client.aclose()

    assert evidence.pages_fetched == 2
    assert not evidence.max_pages_reached
    assert evidence.state is SourceRangeState.COMPLETE


@pytest.mark.asyncio
async def test_repeated_cursor_is_incomplete():
    pages = {
        None: ([api_item("1", "2026-09-08T03:00:00Z")], "c1"),
        "c1": ([api_item("2", "2026-09-07T05:00:00Z")], "c1"),
    }
    service, client = make_service(cursor_handler(pages))
    try:
        _, evidence, _ = await service._paginate_primary(
            ITEM_PARAMS,
            report_start=REPORT_START,
        )
    finally:
        await client.aclose()

    assert evidence.repeated_cursor
    assert evidence.state is SourceRangeState.INCOMPLETE


@pytest.mark.asyncio
@pytest.mark.parametrize("published_at", [None, "malformed", "2026-09-07T05:00:00"])
async def test_missing_malformed_or_naive_published_at_is_incomplete(published_at):
    pages = {None: ([api_item("bad", published_at)], None)}
    service, client = make_service(cursor_handler(pages))
    try:
        _, evidence, _ = await service._paginate_primary(
            ITEM_PARAMS,
            report_start=REPORT_START,
        )
    finally:
        await client.aclose()

    assert evidence.invalid_published_at_items == 1
    assert evidence.state is SourceRangeState.INCOMPLETE


@pytest.mark.asyncio
async def test_actual_ordering_mismatch_is_incomplete():
    pages = {
        None: (
            [
                api_item("older", "2026-09-07T05:00:00Z"),
                api_item("newer", "2026-09-07T06:00:00Z"),
            ],
            None,
        )
    }
    service, client = make_service(cursor_handler(pages))
    try:
        _, evidence, _ = await service._paginate_primary(
            ITEM_PARAMS,
            report_start=REPORT_START,
        )
    finally:
        await client.aclose()

    assert not evidence.ordering_verified
    assert evidence.state is SourceRangeState.INCOMPLETE


@pytest.mark.asyncio
async def test_query_identity_mismatch_is_incomplete():
    pages = {None: ([api_item("1", "2026-09-07T03:59:59Z")], None)}
    handler = cursor_handler(pages, query_overrides={"ordering": "discoveredAtDesc"})
    service, client = make_service(handler)
    try:
        _, evidence, _ = await service._paginate_primary(
            ITEM_PARAMS,
            report_start=REPORT_START,
        )
    finally:
        await client.aclose()

    assert not evidence.query_verified
    assert evidence.state is SourceRangeState.INCOMPLETE


@pytest.mark.asyncio
async def test_page_metadata_mismatch_is_incomplete():
    pages = {None: ([api_item("1", "2026-09-07T03:59:59Z")], None)}
    handler = cursor_handler(pages, page_overrides={"count": 999})
    service, client = make_service(handler)
    try:
        _, evidence, _ = await service._paginate_primary(
            ITEM_PARAMS,
            report_start=REPORT_START,
        )
    finally:
        await client.aclose()

    assert not evidence.page_metadata_verified
    assert evidence.state is SourceRangeState.INCOMPLETE


@pytest.mark.asyncio
async def test_empty_exhausted_api_page_is_proven_complete():
    service, client = make_service(cursor_handler({None: ([], None)}))
    try:
        items, evidence, _ = await service._paginate_primary(
            ITEM_PARAMS,
            report_start=REPORT_START,
        )
    finally:
        await client.aclose()

    assert items == []
    assert evidence.proof_basis is SourceRangeProofBasis.EXHAUSTED
    assert evidence.state is SourceRangeState.COMPLETE


@pytest.mark.asyncio
async def test_api_failure_rss_data_remains_formally_incomplete():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/items":
            return httpx.Response(503)
        if request.url.path == "/feed/all.xml":
            return httpx.Response(200, text=rss())
        raise AssertionError(request.url)

    service, client = make_service(handler)
    try:
        result = await service._primary_channel(
            "all",
            ITEM_PARAMS,
            "/feed/all.xml",
            REPORT_START,
            REPORT_END,
        )
    finally:
        await client.aclose()

    assert result.coverage["status"] == "fallback"
    assert result.coverage["source"] == "rss"
    assert result.coverage["items"] == 1
    assert result.coverage["source_range"]["state"] == "INCOMPLETE"


@pytest.mark.asyncio
async def test_rss_fallback_cannot_make_whole_candidate_formally_complete():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/items":
            if request_query(request).get("mode") == "selected":
                return httpx.Response(503)
            return items_response(request, [], None)
        if request.url.path == "/feed.xml":
            return httpx.Response(200, text=rss())
        if request.url.path == "/api/v1/hot-topics":
            return httpx.Response(200, json={"schemaVersion": 1, "items": []})
        if request.url.path == "/api/v1/dailies/latest":
            return httpx.Response(
                200,
                json={"schemaVersion": 1, "report": {"date": "2026-09-08"}},
            )
        raise AssertionError(request.url)

    service, client = make_service(handler)
    try:
        result = await service.candidate_for(REPORT_DAY)
    finally:
        await client.aclose()

    evaluation = evaluate_candidate_completeness(result.payload)
    assert result.payload["coverage"]["selected"]["status"] == "fallback"
    assert evaluation.state is CandidateCompletenessState.INVALID
    assert evaluation.reason is not None
    assert evaluation.reason.value == "SOURCE_RANGE_INCOMPLETE"


@pytest.mark.asyncio
async def test_strict_window_filter_uses_only_published_at():
    page_items = [
        api_item("after", "2026-09-08T04:00:01Z"),
        api_item("end", "2026-09-08T04:00:00Z"),
        api_item("inside", "2026-09-08T03:59:59Z"),
        api_item("start", "2026-09-07T04:00:00Z"),
        api_item(
            "before",
            "2026-09-07T03:59:59Z",
            discovered_at="2026-09-07T05:00:00Z",
        ),
    ]
    service, client = make_service(cursor_handler({None: (page_items, None)}))
    try:
        result = await service._primary_channel(
            "all", ITEM_PARAMS, "/feed/all.xml", REPORT_START, REPORT_END
        )
    finally:
        await client.aclose()

    assert [item["id"] for item in result.items] == ["inside", "start"]
    assert result.coverage["source_range"]["state"] == "COMPLETE"


@pytest.mark.asyncio
async def test_collected_time_cannot_rescue_missing_published_at():
    item = api_item("missing", None, discovered_at="2026-09-07T05:00:00Z")
    service, client = make_service(cursor_handler({None: ([item], None)}))
    try:
        result = await service._primary_channel(
            "all", ITEM_PARAMS, "/feed/all.xml", REPORT_START, REPORT_END
        )
    finally:
        await client.aclose()

    assert result.items == ()
    assert result.coverage["source_range"]["invalid_published_at_items"] == 1
    assert result.coverage["source_range"]["state"] == "INCOMPLETE"


def complete_service_handler(
    *,
    published_at: str,
    target_report_date: date,
    reverse_equal_items: bool = False,
):
    equal_items = [
        api_item("b", published_at),
        api_item("a", published_at),
    ]
    if reverse_equal_items:
        equal_items.reverse()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/items":
            return items_response(request, equal_items, None)
        if request.url.path == "/api/v1/hot-topics":
            return httpx.Response(200, json={"schemaVersion": 1, "items": []})
        if request.url.path == "/api/v1/dailies/latest":
            return httpx.Response(
                200,
                json={
                    "schemaVersion": 1,
                    "report": {"date": target_report_date.isoformat(), "links": {}},
                },
            )
        raise AssertionError(request.url)

    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retrieval_as_of",
    [
        "2026-09-08T06:00:00Z",
        "2026-09-08T14:00:00Z",
        "2026-09-08T17:00:00Z",
    ],
)
async def test_runtime_delay_does_not_change_report_window(retrieval_as_of: str):
    service, client = make_service(
        complete_service_handler(
            published_at="2026-09-07T05:00:00Z",
            target_report_date=REPORT_DAY,
        ),
        clock=lambda: utc(retrieval_as_of),
    )
    try:
        result = await service.candidate_for(REPORT_DAY)
    finally:
        await client.aclose()

    assert result.payload["report_window"] == {
        "from": "2026-09-07T04:00:00Z",
        "to": "2026-09-08T04:00:00Z",
        "timezone": "Asia/Shanghai",
        "bounds": "[from,to)",
    }
    assert evaluate_candidate_completeness(result.payload).state is (
        CandidateCompletenessState.VALID_COMPLETE
    )


@pytest.mark.asyncio
async def test_historical_target_does_not_fetch_current_supplementary_channels():
    target = date(2026, 9, 4)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/items"
        return items_response(request, [], None)

    service, client = make_service(handler, clock=lambda: utc("2026-09-08T05:00:00Z"))
    try:
        result = await service.candidate_for(target)
    finally:
        await client.aclose()

    assert result.logical_requests == 3
    assert result.payload["coverage"]["hot_topics"]["status"] == (
        "unavailable_for_target"
    )
    assert result.payload["coverage"]["daily"]["status"] == (
        "unavailable_for_target"
    )
    assert evaluate_candidate_completeness(result.payload).state is (
        CandidateCompletenessState.VALID_COMPLETE
    )


@pytest.mark.asyncio
async def test_equal_timestamp_input_order_has_deterministic_semantic_output():
    payloads = []
    for reverse in (False, True):
        service, client = make_service(
            complete_service_handler(
                published_at="2026-09-07T05:00:00Z",
                target_report_date=REPORT_DAY,
                reverse_equal_items=reverse,
            )
        )
        try:
            payloads.append((await service.candidate_for(REPORT_DAY)).payload)
        finally:
            await client.aclose()

    assert payloads[0]["items"] == payloads[1]["items"]
    assert payloads[0]["coverage"] == payloads[1]["coverage"]
    assert payloads[0]["summary"] == payloads[1]["summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retrieval_as_of",
    [
        "2026-08-28T05:10:00Z",
        "2026-08-28T15:32:00Z",
        "2026-08-28T16:22:00Z",
        "2026-08-28T17:10:00Z",
    ],
)
async def test_incident_data_replay_keeps_original_d_and_item_set(retrieval_as_of: str):
    target = date(2026, 8, 28)
    service, client = make_service(
        complete_service_handler(
            published_at="2026-08-27T05:00:00Z",
            target_report_date=target,
        ),
        clock=lambda: utc(retrieval_as_of),
    )
    try:
        result = await service.candidate_for(target)
    finally:
        await client.aclose()

    assert result.payload["target_report_date"] == "2026-08-28"
    assert result.payload["report_window"]["from"] == "2026-08-27T04:00:00Z"
    assert result.payload["report_window"]["to"] == "2026-08-28T04:00:00Z"
    assert [item["id"] for item in result.payload["items"]] == ["a", "b"]
    assert evaluate_candidate_completeness(result.payload).state is (
        CandidateCompletenessState.VALID_COMPLETE
    )

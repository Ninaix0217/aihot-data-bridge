from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import httpx
import pytest

from aihot_bridge.config import Settings
from aihot_bridge.service import BridgeService
from aihot_bridge.timefields import published_at_in_window
from aihot_bridge.upstream import UpstreamClient
from tests.test_bridge import success_handler


CST = timezone(timedelta(hours=8), name="Asia/Shanghai")


def report_window(report_day: date) -> tuple[datetime, datetime]:
    end = datetime.combine(report_day, time(12, 0), tzinfo=CST)
    return end - timedelta(days=1), end


def test_original_rolling_24h_misses_1223_window_prefix():
    snapshot_time = datetime(2026, 8, 18, 12, 23, tzinfo=CST)
    report_start, _ = report_window(date(2026, 8, 18))
    rolling_start = snapshot_time - timedelta(hours=24)

    assert rolling_start == datetime(2026, 8, 17, 12, 23, tzinfo=CST)
    assert report_start < rolling_start
    assert rolling_start - report_start == timedelta(minutes=23)


async def snapshot_at(moment: datetime) -> dict:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(success_handler),
        base_url="https://aihot.virxact.com",
    )
    service = BridgeService(
        UpstreamClient(client, max_retries=0),
        Settings(max_retries=0),
        clock=lambda: moment,
    )
    try:
        return await service.today()
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "generated_at",
    (
        datetime(2026, 8, 18, 12, 23, tzinfo=CST),
        datetime(2026, 8, 18, 13, 10, tzinfo=CST),
    ),
)
async def test_30_hour_candidate_covers_fixed_daily_window(
    generated_at: datetime,
):
    payload = await snapshot_at(generated_at)
    report_start, report_end = report_window(date(2026, 8, 18))
    candidate_start = datetime.fromisoformat(
        payload["window"]["from"].replace("Z", "+00:00")
    )
    candidate_end = datetime.fromisoformat(
        payload["window"]["to"].replace("Z", "+00:00")
    )

    assert candidate_start <= report_start.astimezone(timezone.utc)
    assert candidate_end >= report_end.astimezone(timezone.utc)


def test_fixed_window_is_start_inclusive_and_end_exclusive():
    start, end = report_window(date(2026, 8, 18))
    cases = {
        "before": "2026-08-17T11:59:59+08:00",
        "start": "2026-08-17T12:00:00+08:00",
        "last": "2026-08-18T11:59:59+08:00",
        "end": "2026-08-18T12:00:00+08:00",
    }

    included = {
        name
        for name, published_at in cases.items()
        if published_at_in_window({"published_at": published_at}, start, end)
    }

    assert included == {"start", "last"}


def test_missing_published_at_is_not_replaced_by_collected_at():
    start, end = report_window(date(2026, 8, 18))
    item = {
        "published_at": None,
        "collected_at": "2026-08-18T03:00:00Z",
    }

    assert not published_at_in_window(item, start, end)


def test_utc_and_asia_shanghai_timestamps_have_identical_membership():
    start, end = report_window(date(2026, 8, 18))
    utc_item = {"published_at": "2026-08-17T04:00:00Z"}
    cst_item = {"published_at": "2026-08-17T12:00:00+08:00"}

    assert published_at_in_window(utc_item, start, end)
    assert published_at_in_window(cst_item, start, end)

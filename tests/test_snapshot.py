from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from aihot_bridge.config import Settings
from aihot_bridge.service import BridgeService
from aihot_bridge.snapshot import (
    SnapshotRejected,
    export_snapshot,
    load_snapshot,
    main,
    report_candidate_path,
    write_snapshot,
)
from aihot_bridge.upstream import UpstreamClient
from tests.test_bridge import NOW, success_handler


def snapshot_payload(
    *,
    selected_status: str = "ok",
    selected_items: int = 1,
    all_status: str = "ok",
    all_items: int = 2,
    paper_status: str = "ok",
    paper_items: int = 1,
) -> dict:
    coverage = {
        "selected": {
            "status": selected_status,
            "source": "api" if selected_status != "failed" else None,
            "items": selected_items,
            "error": None,
        },
        "all": {
            "status": all_status,
            "source": "api" if all_status != "failed" else None,
            "items": all_items,
            "error": None,
        },
        "paper": {
            "status": paper_status,
            "source": "api" if paper_status != "failed" else None,
            "items": paper_items,
            "error": None,
        },
        "hot_topics": {"status": "ok", "source": "api", "items": 1, "error": None},
        "daily": {"status": "ok", "source": "api", "items": 1, "error": None},
    }
    items = [
        {
            "id": "item-1",
            "item_type": "item",
            "category": "industry",
            "title": "Snapshot item",
            "description": None,
            "original_url": "https://example.com/item-1",
            "aihot_url": "https://aihot.virxact.com/items/item-1",
            "published_at": "2026-08-16T12:00:00Z",
            "collected_at": "2026-08-16T12:05:00Z",
            "source": {"name": "Example"},
            "source_channels": ["all"],
            "metadata": {},
        }
    ]
    return {
        "schema_version": "aihot-bridge/v1",
        "generated_at": "2026-08-17T00:00:05Z",
        "window": {
            "type": "rolling_candidate",
            "hours": 30,
            "from": "2026-08-15T18:00:00Z",
            "to": "2026-08-17T00:00:00Z",
        },
        "coverage": coverage,
        "summary": {"raw_items": 6, "deduplicated_items": 1},
        "items": items,
    }


class FakeService:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    async def today(self) -> dict:
        self.calls += 1
        return self.payload


@pytest.mark.asyncio
async def test_exporter_calls_existing_service_and_writes_snapshot(tmp_path: Path):
    service = FakeService(snapshot_payload())
    output = tmp_path / "dist" / "today.json"

    result = await export_snapshot(service, output)

    assert service.calls == 1
    assert result == service.payload
    assert load_snapshot(output) == service.payload
    assert json.loads(output.read_text(encoding="utf-8"))["schema_version"] == "aihot-bridge/v1"
    daily = tmp_path / "dist" / "report-candidate" / "2026-08-17.json"
    assert load_snapshot(daily) == service.payload
    assert output.read_bytes() == daily.read_bytes()
    assert {
        path.relative_to(tmp_path / "dist").as_posix()
        for path in (tmp_path / "dist").rglob("*.json")
    } == {"today.json", "report-candidate/2026-08-17.json"}


@pytest.mark.parametrize(
    "generated_at",
    (
        "2026-08-17T16:30:00Z",
        "2026-08-18T00:30:00+08:00",
    ),
)
def test_report_candidate_path_uses_beijing_date_boundary(
    tmp_path: Path, generated_at: str
):
    output = tmp_path / "dist" / "today.json"

    assert report_candidate_path(output, generated_at) == (
        tmp_path / "dist" / "report-candidate" / "2026-08-18.json"
    )


def test_partial_coverage_is_publishable(tmp_path: Path):
    payload = snapshot_payload(
        selected_status="ok",
        selected_items=1,
        all_status="failed",
        all_items=0,
        paper_status="fallback",
        paper_items=1,
    )
    output = tmp_path / "today.json"

    write_snapshot(payload, output)

    assert load_snapshot(output)["coverage"]["paper"]["status"] == "fallback"


def test_complete_major_failure_preserves_last_good_snapshot(tmp_path: Path):
    output = tmp_path / "today.json"
    output.write_text('{"previous":"snapshot"}\n', encoding="utf-8")
    failed = snapshot_payload(
        selected_status="failed",
        selected_items=0,
        all_status="failed",
        all_items=0,
        paper_status="failed",
        paper_items=0,
    )

    with pytest.raises(SnapshotRejected, match="all major data channels"):
        write_snapshot(failed, output)

    assert output.read_text(encoding="utf-8") == '{"previous":"snapshot"}\n'


@pytest.mark.asyncio
async def test_complete_major_failure_creates_no_new_snapshot(tmp_path: Path):
    output = tmp_path / "dist" / "today.json"
    failed = snapshot_payload(
        selected_status="failed",
        selected_items=0,
        all_status="failed",
        all_items=0,
        paper_status="failed",
        paper_items=0,
    )
    service = FakeService(failed)

    with pytest.raises(SnapshotRejected, match="all major data channels"):
        await export_snapshot(service, output)

    assert service.calls == 1
    assert not output.exists()
    assert not (tmp_path / "dist" / "report-candidate").exists()


def test_cli_returns_nonzero_for_complete_major_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    failed = snapshot_payload(
        selected_status="failed",
        selected_items=0,
        all_status="failed",
        all_items=0,
        paper_status="failed",
        paper_items=0,
    )
    snapshot = tmp_path / "failed.json"
    snapshot.write_text(json.dumps(failed), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["aihot-snapshot", "--check", str(snapshot)])

    assert main() == 1


@pytest.mark.asyncio
async def test_generated_at_is_snapshot_completion_time():
    completed_at = NOW + timedelta(seconds=5)
    clock_values = iter((NOW, completed_at))
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(success_handler),
        base_url="https://aihot.virxact.com",
    )
    service = BridgeService(
        UpstreamClient(client, max_retries=0),
        Settings(max_retries=0),
        clock=lambda: next(clock_values),
    )
    try:
        payload = await service.today()
    finally:
        await client.aclose()

    assert payload["window"]["to"] == "2026-08-17T00:00:00Z"
    assert payload["generated_at"] == "2026-08-17T00:00:05Z"
    assert datetime.fromisoformat(payload["generated_at"].replace("Z", "+00:00")) == completed_at

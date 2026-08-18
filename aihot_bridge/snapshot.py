from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from .config import Settings
from .service import BridgeService
from .upstream import UpstreamClient


logger = logging.getLogger(__name__)

REQUIRED_CHANNELS = ("selected", "all", "paper", "hot_topics", "daily")
MAJOR_CHANNELS = ("selected", "all", "paper")
TRUSTED_STATUSES = {"ok", "fallback", "partial"}


class SnapshotRejected(RuntimeError):
    """The current fetch is not safe to publish as a fresh snapshot."""


class TodayService(Protocol):
    async def today(self) -> dict[str, Any]: ...


async def export_snapshot(
    service: TodayService, output_path: Path
) -> dict[str, Any]:
    payload = await service.today()
    write_snapshot(payload, output_path)
    return payload


def write_snapshot(payload: dict[str, Any], output_path: Path) -> None:
    validate_snapshot(payload)
    ensure_trustworthy_snapshot(payload)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def load_snapshot(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotRejected(f"cannot read snapshot: {exc}") from exc
    if not isinstance(payload, dict):
        raise SnapshotRejected("snapshot JSON root must be an object")
    validate_snapshot(payload)
    ensure_trustworthy_snapshot(payload)
    return payload


def validate_snapshot(payload: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "generated_at",
        "window",
        "coverage",
        "summary",
        "items",
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise SnapshotRejected(f"missing required fields: {', '.join(missing)}")
    if payload["schema_version"] != "aihot-bridge/v1":
        raise SnapshotRejected("unexpected schema_version")
    if not isinstance(payload["window"], dict):
        raise SnapshotRejected("window must be an object")
    if not isinstance(payload["coverage"], dict):
        raise SnapshotRejected("coverage must be an object")
    if not isinstance(payload["summary"], dict):
        raise SnapshotRejected("summary must be an object")
    if not isinstance(payload["items"], list):
        raise SnapshotRejected("items must be an array")

    generated_at = payload["generated_at"]
    if not isinstance(generated_at, str):
        raise SnapshotRejected("generated_at must be an ISO 8601 string")
    try:
        parsed_generated_at = datetime.fromisoformat(
            generated_at.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise SnapshotRejected("generated_at must be valid ISO 8601") from exc
    if parsed_generated_at.tzinfo is None:
        raise SnapshotRejected("generated_at must include a timezone")

    coverage = payload["coverage"]
    for channel in REQUIRED_CHANNELS:
        entry = coverage.get(channel)
        if not isinstance(entry, dict):
            raise SnapshotRejected(f"coverage.{channel} must be an object")
        if not isinstance(entry.get("status"), str):
            raise SnapshotRejected(f"coverage.{channel}.status must be a string")
        if not isinstance(entry.get("items"), int) or entry["items"] < 0:
            raise SnapshotRejected(
                f"coverage.{channel}.items must be a non-negative integer"
            )

    summary = payload["summary"]
    for field in ("raw_items", "deduplicated_items"):
        if not isinstance(summary.get(field), int) or summary[field] < 0:
            raise SnapshotRejected(f"summary.{field} must be a non-negative integer")


def ensure_trustworthy_snapshot(payload: dict[str, Any]) -> None:
    coverage = payload["coverage"]
    credible_major_channel = any(
        coverage[channel]["status"] in TRUSTED_STATUSES
        and coverage[channel]["items"] > 0
        for channel in MAJOR_CHANNELS
    )
    if not credible_major_channel:
        raise SnapshotRejected(
            "all major data channels lack trustworthy items; refusing to replace the last good snapshot"
        )
    if payload["summary"]["deduplicated_items"] <= 0:
        raise SnapshotRejected(
            "snapshot contains no trustworthy deduplicated items; refusing publication"
        )


def log_snapshot_summary(payload: dict[str, Any]) -> None:
    print(f"generated_at: {payload['generated_at']}")
    window = payload["window"]
    print(
        f"candidate_window: [{window.get('from')}, {window.get('to')}) "
        f"hours={window.get('hours')}"
    )
    for channel in REQUIRED_CHANNELS:
        entry = payload["coverage"][channel]
        print(
            f"{channel}: status={entry['status']} "
            f"source={entry.get('source')} items={entry['items']}"
        )
    summary = payload["summary"]
    print(f"raw_items: {summary['raw_items']}")
    print(f"deduplicated_items: {summary['deduplicated_items']}")


async def build_live_snapshot(output_path: Path) -> dict[str, Any]:
    settings = Settings.from_env()
    timeout = httpx.Timeout(
        settings.request_timeout_seconds,
        connect=settings.connect_timeout_seconds,
    )
    async with httpx.AsyncClient(
        base_url=settings.api_base_url,
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "aihot-data-bridge/0.1"},
    ) as client:
        service = BridgeService(
            UpstreamClient(client, max_retries=settings.max_retries),
            settings,
        )
        return await export_snapshot(service, output_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build or validate AIHOT today.json")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        type=Path,
        metavar="PATH",
        help="validate an existing snapshot without fetching upstream data",
    )
    mode.add_argument(
        "--output",
        type=Path,
        default=Path("dist/today.json"),
        help="snapshot output path (default: dist/today.json)",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parse_args()
    try:
        if args.check is not None:
            payload = load_snapshot(args.check)
        else:
            payload = asyncio.run(build_live_snapshot(args.output))
        log_snapshot_summary(payload)
        return 0
    except (SnapshotRejected, httpx.HTTPError, OSError) as exc:
        logger.error("snapshot build failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

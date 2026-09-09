from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

from aihot_bridge.candidate_v2 import (
    PRIMARY_CHANNELS,
    SourceRangeProofBasis,
    format_timestamp,
    make_source_range_evidence,
)
from aihot_bridge.logical_date import report_window_for_date


def complete_candidate_payload(
    *,
    report_day: date = date(2026, 9, 8),
    empty: bool = False,
) -> dict:
    report_start, report_end = report_window_for_date(report_day)
    start_utc = report_start.astimezone(timezone.utc)
    end_utc = report_end.astimezone(timezone.utc)
    as_of = end_utc + timedelta(hours=1)
    generated_at = as_of + timedelta(minutes=1)
    evidence = make_source_range_evidence(
        report_start=start_utc,
        applicable=True,
        proof_basis=SourceRangeProofBasis.EXHAUSTED,
        pages_fetched=1,
        oldest_published_at=None if empty else start_utc,
        cursor_exhausted=True,
        ordering_verified=True,
        query_verified=True,
        page_metadata_verified=True,
    ).to_payload()
    supplementary = make_source_range_evidence(
        report_start=None,
        applicable=False,
    ).to_payload()
    item = {
        "id": "selected-1",
        "item_type": "item",
        "category": "ai-models",
        "title": "Candidate",
        "description": None,
        "original_url": "https://example.com/candidate",
        "aihot_url": "https://aihot.virxact.com/items/selected-1",
        "published_at": format_timestamp(start_utc),
        "collected_at": format_timestamp(start_utc + timedelta(minutes=10)),
        "source": {"name": "Example"},
        "source_channels": ["selected"],
        "metadata": {},
    }
    primary_counts = {channel: 0 for channel in PRIMARY_CHANNELS}
    if not empty:
        primary_counts["selected"] = 1
    coverage = {
        channel: {
            "status": "ok",
            "source": "api",
            "items": primary_counts[channel],
            "source_range": deepcopy(evidence),
        }
        for channel in PRIMARY_CHANNELS
    }
    coverage.update(
        {
            channel: {
                "status": "unavailable_for_target",
                "source": None,
                "items": 0,
                "source_range": deepcopy(supplementary),
            }
            for channel in ("hot_topics", "daily")
        }
    )
    items = [] if empty else [item]
    return {
        "schema_version": "aihot-bridge/v2",
        "target_report_date": report_day.isoformat(),
        "generated_at": format_timestamp(generated_at),
        "report_window": {
            "from": format_timestamp(start_utc),
            "to": format_timestamp(end_utc),
            "timezone": "Asia/Shanghai",
            "bounds": "[from,to)",
        },
        "retrieval": {
            "as_of": format_timestamp(as_of),
            "upstream_window": "7d",
            "by": "published",
        },
        "coverage": coverage,
        "summary": {
            "raw_items": len(items),
            "deduplicated_items": len(items),
        },
        "items": items,
    }

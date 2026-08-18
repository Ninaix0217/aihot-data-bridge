from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from aihot_bridge.freshness import (
    FreshnessCheckError,
    SnapshotDocument,
    check_matching_public_snapshots,
    evaluate_snapshot,
    fetch_snapshot,
)
from tests.test_snapshot import snapshot_payload


NOW = datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc)


def test_fresh_snapshot_passes():
    result = evaluate_snapshot(
        {"generated_at": "2026-08-17T23:30:00Z"},
        now=NOW,
        max_age=timedelta(minutes=90),
    )

    assert result.age_seconds == 30 * 60


def test_stale_snapshot_fails():
    with pytest.raises(FreshnessCheckError, match="snapshot is stale"):
        evaluate_snapshot(
            {"generated_at": "2026-08-17T22:29:59Z"},
            now=NOW,
            max_age=timedelta(minutes=90),
        )


@pytest.mark.parametrize(
    "payload, message",
    [
        ({}, "missing"),
        ({"generated_at": "not-a-timestamp"}, "not valid ISO 8601"),
    ],
)
def test_missing_or_malformed_generated_at_fails(payload: dict, message: str):
    with pytest.raises(FreshnessCheckError, match=message):
        evaluate_snapshot(
            payload,
            now=NOW,
            max_age=timedelta(minutes=90),
        )


def test_expected_deployment_must_be_public():
    with pytest.raises(FreshnessCheckError, match="has not reached"):
        evaluate_snapshot(
            {"generated_at": "2026-08-17T23:30:00Z"},
            now=NOW,
            max_age=timedelta(minutes=90),
            expected_generated_at="2026-08-17T23:45:00Z",
        )


def test_http_failure_is_reported():
    def failing_opener(*_args, **_kwargs):
        raise urllib.error.URLError("network unavailable")

    with pytest.raises(FreshnessCheckError, match="request failed"):
        fetch_snapshot("https://example.com/today.json", opener=failing_opener)


def test_malformed_json_response_is_reported():
    class FakeResponse(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def malformed_opener(*_args, **_kwargs):
        return FakeResponse(b"not-json")

    with pytest.raises(FreshnessCheckError, match="not valid JSON"):
        fetch_snapshot(
            "https://example.com/today.json", opener=malformed_opener
        )


def test_matching_public_snapshots_require_same_valid_bytes(
    monkeypatch: pytest.MonkeyPatch,
):
    payload = snapshot_payload()
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )

    monkeypatch.setattr(
        "aihot_bridge.freshness.fetch_snapshot_document",
        lambda *_args, **_kwargs: SnapshotDocument(payload, content),
    )

    result = check_matching_public_snapshots(
        "https://example.com/today.json",
        "https://example.com/report-candidate/2026-08-17.json",
        now_factory=lambda: datetime(2026, 8, 17, 0, 1, tzinfo=timezone.utc),
        max_age=timedelta(minutes=90),
        expected_generated_at=payload["generated_at"],
    )

    assert result.canonical.payload == payload
    assert result.dated.payload == payload
    assert len(result.sha256) == 64


def test_matching_public_snapshots_reject_different_bytes(
    monkeypatch: pytest.MonkeyPatch,
):
    payload = snapshot_payload()
    documents = iter(
        (
            SnapshotDocument(payload, b"canonical"),
            SnapshotDocument(payload, b"dated"),
        )
    )
    monkeypatch.setattr(
        "aihot_bridge.freshness.fetch_snapshot_document",
        lambda *_args, **_kwargs: next(documents),
    )

    with pytest.raises(FreshnessCheckError, match="not byte-identical"):
        check_matching_public_snapshots(
            "https://example.com/today.json",
            "https://example.com/report-candidate/2026-08-17.json",
            now_factory=lambda: datetime(2026, 8, 17, 0, 1, tzinfo=timezone.utc),
            max_age=timedelta(minutes=90),
            expected_generated_at=payload["generated_at"],
        )

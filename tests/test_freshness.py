from __future__ import annotations

import hashlib
import io
import json
import urllib.error
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aihot_bridge.freshness import (
    FreshnessCheckError,
    PublicVerificationError,
    SnapshotDocument,
    _write_matching_github_summary,
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


CANONICAL_URL = "https://example.com/today.json"
DATED_URL = "https://example.com/report-candidate/2026-08-18.json"
VERIFY_NOW = datetime(2026, 8, 18, 15, 1, tzinfo=timezone.utc)
INCIDENT_EXPECTED = "2026-08-18T14:59:35.209873Z"
INCIDENT_OBSERVED = "2026-08-18T14:19:43.601390Z"


def _snapshot_document(
    generated_at: str,
    *,
    title: str = "Snapshot item",
) -> SnapshotDocument:
    payload = deepcopy(snapshot_payload())
    payload["generated_at"] = generated_at
    payload["items"][0]["title"] = title
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return SnapshotDocument(payload, content)


def _sha256(document: SnapshotDocument) -> str:
    return hashlib.sha256(document.content).hexdigest()


def _install_observations(
    monkeypatch: pytest.MonkeyPatch,
    *,
    canonical: list[SnapshotDocument | Exception],
    dated: list[SnapshotDocument | Exception],
) -> None:
    observations = {
        CANONICAL_URL: iter(canonical),
        DATED_URL: iter(dated),
    }

    def fake_fetch(url: str, **_kwargs) -> SnapshotDocument:
        observation = next(observations[url])
        if isinstance(observation, Exception):
            raise observation
        return observation

    monkeypatch.setattr(
        "aihot_bridge.freshness.fetch_snapshot_document",
        fake_fetch,
    )


def _check(
    expected: SnapshotDocument,
    *,
    attempts: int = 1,
):
    return check_matching_public_snapshots(
        CANONICAL_URL,
        DATED_URL,
        now_factory=lambda: VERIFY_NOW,
        max_age=timedelta(minutes=90),
        expected_generated_at=expected.payload["generated_at"],
        expected_sha256=_sha256(expected),
        attempts=attempts,
        interval_seconds=0,
    )


def test_dated_and_canonical_verified_confirms_public_parity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    expected = _snapshot_document(INCIDENT_EXPECTED)
    _install_observations(
        monkeypatch,
        canonical=[expected],
        dated=[expected],
    )

    result = _check(expected)

    assert result.state == "PUBLIC_PARITY_CONFIRMED"
    assert result.dated_observation.result == "DATED_VERIFIED"
    assert result.canonical_observation.result == "VERIFIED"
    assert result.attempts_used == 1

    summary_path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    _write_matching_github_summary(result)
    summary = summary_path.read_text(encoding="utf-8")
    assert "Expected generated_at" in summary
    assert "Dated consumer (HARD SLO)" in summary
    assert "Canonical (SOFT PARITY)" in summary
    assert "PUBLIC_PARITY_CONFIRMED" in summary
    assert "Attempts used: `1`" in summary


def test_dated_verified_canonical_stale_succeeds_with_lag_warning(
    monkeypatch: pytest.MonkeyPatch,
):
    expected = _snapshot_document(INCIDENT_EXPECTED)
    stale = _snapshot_document(INCIDENT_OBSERVED)
    _install_observations(monkeypatch, canonical=[stale], dated=[expected])

    result = _check(expected)

    assert result.state == "CANONICAL_PROPAGATION_LAG"
    assert result.dated_observation.result == "DATED_VERIFIED"
    assert result.canonical_observation.result == "PROPAGATION_LAG"


def test_dated_stale_canonical_verified_is_hard_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    expected = _snapshot_document(INCIDENT_EXPECTED)
    stale = _snapshot_document(INCIDENT_OBSERVED)
    _install_observations(monkeypatch, canonical=[expected], dated=[stale])

    with pytest.raises(PublicVerificationError) as raised:
        _check(expected)

    assert raised.value.result.state == "DATED_PROPAGATION_TIMEOUT"
    assert raised.value.result.canonical_observation.result == "VERIFIED"


def test_dated_propagation_lag_then_update_succeeds_and_records_attempts(
    monkeypatch: pytest.MonkeyPatch,
):
    expected = _snapshot_document(INCIDENT_EXPECTED)
    stale = _snapshot_document(INCIDENT_OBSERVED)
    _install_observations(
        monkeypatch,
        canonical=[stale, expected],
        dated=[stale, expected],
    )

    result = _check(expected, attempts=2)

    assert result.state == "PUBLIC_PARITY_CONFIRMED"
    assert result.attempts_used == 2


def test_production_incident_dated_stays_stale_until_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
):
    expected = _snapshot_document(INCIDENT_EXPECTED)
    stale = _snapshot_document(INCIDENT_OBSERVED)
    _install_observations(
        monkeypatch,
        canonical=[stale] * 12,
        dated=[stale] * 12,
    )

    with pytest.raises(PublicVerificationError) as raised:
        _check(expected, attempts=12)

    result = raised.value.result
    assert result.state == "DATED_PROPAGATION_TIMEOUT"
    assert result.attempts_used == 12
    assert result.dated_observation.generated_at == INCIDENT_OBSERVED
    assert result.expected_generated_at == INCIDENT_EXPECTED


def test_canonical_http_error_does_not_block_verified_dated_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    expected = _snapshot_document(INCIDENT_EXPECTED)
    _install_observations(
        monkeypatch,
        canonical=[FreshnessCheckError("public snapshot returned HTTP 503")],
        dated=[expected],
    )

    result = _check(expected)

    assert result.state == "CANONICAL_PUBLIC_READ_ERROR"
    assert result.canonical_observation.result == "PUBLIC_READ_ERROR"
    assert result.dated_observation.result == "DATED_VERIFIED"
    attempt_log = capsys.readouterr().err
    assert "canonical: result=PUBLIC_READ_ERROR" in attempt_log
    assert "dated: result=DATED_VERIFIED" in attempt_log
    assert f"expected_generated_at={INCIDENT_EXPECTED}" in attempt_log


@pytest.mark.parametrize(
    "dated_observation",
    [
        FreshnessCheckError("public snapshot returned HTTP 503"),
        FreshnessCheckError("public snapshot is not valid JSON"),
        SnapshotDocument({}, b"{}"),
    ],
)
def test_dated_public_read_or_schema_error_is_hard_failure(
    monkeypatch: pytest.MonkeyPatch,
    dated_observation: SnapshotDocument | Exception,
):
    expected = _snapshot_document(INCIDENT_EXPECTED)
    _install_observations(
        monkeypatch,
        canonical=[expected],
        dated=[dated_observation],
    )

    with pytest.raises(PublicVerificationError) as raised:
        _check(expected)

    assert raised.value.result.state == "DATED_PUBLIC_READ_ERROR"


def test_canonical_content_mismatch_is_soft_parity_warning(
    monkeypatch: pytest.MonkeyPatch,
):
    expected = _snapshot_document(INCIDENT_EXPECTED)
    different = _snapshot_document(INCIDENT_EXPECTED, title="Different canonical")
    _install_observations(monkeypatch, canonical=[different], dated=[expected])

    result = _check(expected)

    assert result.state == "CANONICAL_PARITY_MISMATCH"
    assert result.dated_observation.result == "DATED_VERIFIED"
    assert result.canonical_observation.result == "PARITY_MISMATCH"


def test_dated_content_mismatch_is_hard_artifact_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    expected = _snapshot_document(INCIDENT_EXPECTED)
    different = _snapshot_document(INCIDENT_EXPECTED, title="Wrong artifact")
    _install_observations(monkeypatch, canonical=[expected], dated=[different])

    with pytest.raises(PublicVerificationError) as raised:
        _check(expected)

    assert raised.value.result.state == "DATED_ARTIFACT_MISMATCH"

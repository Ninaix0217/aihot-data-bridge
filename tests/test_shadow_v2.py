from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from aihot_bridge import repository_v2, snapshot
from aihot_bridge.candidate_v2 import CandidateV2Error
from aihot_bridge.logical_date import LogicalDateError, LogicalDateErrorReason
from aihot_bridge.retrieval_v2 import CandidateBuildResult
from aihot_bridge.shadow_v2 import (
    StartedAtObservation,
    StartedAtSource,
    observe_started_at,
    run_shadow,
)
from tests.v2_fixtures import complete_candidate_payload


PASS_A = "50 4 * * *"
PASS_B = "10 5 * * *"


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def actions_client(handler) -> httpx.Client:
    return httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )


def test_shadow_module_has_no_repository_or_pages_publication_dependency():
    source = (
        Path(__file__).resolve().parents[1] / "aihot_bridge" / "shadow_v2.py"
    ).read_text(encoding="utf-8")

    for forbidden in (
        "repository_v2",
        "github_repository_v2",
        "publish_v2_candidate",
        "GitHubGitDataAdapter",
        "upload_pages",
        "deploy_pages",
    ):
        assert forbidden not in source


def test_actions_run_started_at_is_preferred():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/repos/owner/repo/actions/runs/123"
        return httpx.Response(
            200,
            json={"run_started_at": "2026-09-08T04:51:00Z"},
        )

    with actions_client(handler) as client:
        observed = observe_started_at(
            client,
            repository="owner/repo",
            run_id="123",
            runner_fallback_started_at="2026-09-08T04:52:00Z",
        )

    assert observed == StartedAtObservation(
        utc("2026-09-08T04:51:00Z"),
        StartedAtSource.ACTIONS_RUN_API,
    )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, json={"message": "temporarily unavailable"}),
        httpx.Response(200, json={"run_started_at": "not-a-timestamp"}),
        httpx.Response(200, content=b"not-json"),
    ],
)
def test_actions_run_lookup_failure_uses_labeled_runner_fallback(response):
    with actions_client(lambda _request: response) as client:
        observed = observe_started_at(
            client,
            repository="owner/repo",
            run_id="123",
            runner_fallback_started_at="2026-09-08T04:52:00Z",
        )

    assert observed.started_at == utc("2026-09-08T04:52:00Z")
    assert observed.source is StartedAtSource.RUNNER_FALLBACK
    assert observed.api_error


@pytest.mark.parametrize(
    ("event_schedule", "started_at", "expected_pass"),
    [
        (PASS_A, "2026-09-08T04:51:00Z", "A"),
        (PASS_B, "2026-09-08T05:11:00Z", "B"),
    ],
)
def test_shadow_resolves_real_schedule_identity_and_builds_valid_candidate(
    event_schedule: str,
    started_at: str,
    expected_pass: str,
):
    payload = complete_candidate_payload(report_day=date(2026, 9, 8))
    requested_dates: list[date] = []

    async def builder(report_day: date) -> CandidateBuildResult:
        requested_dates.append(report_day)
        return CandidateBuildResult(payload, logical_requests=7, duration_seconds=0.5)

    result = run_shadow(
        event_schedule=event_schedule,
        started_at=StartedAtObservation(
            utc(started_at),
            StartedAtSource.ACTIONS_RUN_API,
        ),
        candidate_builder=builder,
    )

    assert requested_dates == [date(2026, 9, 8)]
    assert result["trigger"]["pass"] == expected_pass
    assert result["trigger"]["target_report_date"] == "2026-09-08"
    assert result["candidate"]["state"] == "VALID_COMPLETE"
    assert result["repository"] == {"write_attempted": "NO"}
    assert result["v1"] == "NOT_TOUCHED"
    assert result["pages"] == "NOT_USED"


@pytest.mark.parametrize(
    ("started_at", "expected_lag"),
    [
        ("2026-08-28T15:10:00Z", 10 * 60 * 60),
        ("2026-08-28T17:20:46Z", 43846),
    ],
)
def test_shadow_delay_and_beijing_midnight_keep_original_pass_b_date(
    started_at: str,
    expected_lag: int,
):
    payload = complete_candidate_payload(report_day=date(2026, 8, 28))

    async def builder(report_day: date) -> CandidateBuildResult:
        assert report_day == date(2026, 8, 28)
        return CandidateBuildResult(payload, logical_requests=1, duration_seconds=0.1)

    result = run_shadow(
        event_schedule=PASS_B,
        started_at=StartedAtObservation(
            utc(started_at),
            StartedAtSource.ACTIONS_RUN_API,
        ),
        candidate_builder=builder,
    )

    assert result["trigger"]["target_report_date"] == "2026-08-28"
    assert result["trigger"]["schedule_lag_seconds"] == expected_lag
    assert result["report"] == {
        "report_start": "2026-08-27T04:00:00+00:00",
        "report_end": "2026-08-28T04:00:00+00:00",
    }


def test_shadow_accepts_exactly_18_hours():
    payload = complete_candidate_payload(report_day=date(2026, 9, 8))

    async def builder(_report_day: date) -> CandidateBuildResult:
        return CandidateBuildResult(payload, logical_requests=1, duration_seconds=0.1)

    result = run_shadow(
        event_schedule=PASS_A,
        started_at=StartedAtObservation(
            utc("2026-09-08T22:50:00Z"),
            StartedAtSource.ACTIONS_RUN_API,
        ),
        candidate_builder=builder,
    )

    assert result["trigger"]["schedule_lag_seconds"] == 18 * 60 * 60


@pytest.mark.parametrize(
    ("event_schedule", "started_at", "reason"),
    [
        (
            PASS_A,
            "2026-09-08T22:50:01Z",
            LogicalDateErrorReason.SCHEDULE_IDENTITY_UNRESOLVED,
        ),
        (
            "11 5 * * *",
            "2026-09-08T05:11:00Z",
            LogicalDateErrorReason.UNKNOWN_SCHEDULE,
        ),
    ],
)
def test_shadow_identity_failure_is_fail_closed_before_build(
    event_schedule: str,
    started_at: str,
    reason: LogicalDateErrorReason,
):
    calls = 0

    async def builder(_report_day: date) -> CandidateBuildResult:
        nonlocal calls
        calls += 1
        raise AssertionError("builder must not run")

    with pytest.raises(LogicalDateError) as caught:
        run_shadow(
            event_schedule=event_schedule,
            started_at=StartedAtObservation(
                utc(started_at),
                StartedAtSource.ACTIONS_RUN_API,
            ),
            candidate_builder=builder,
        )

    assert caught.value.reason is reason
    assert calls == 0


@pytest.mark.parametrize("incomplete", [False, True])
def test_shadow_never_invokes_repository_or_pages_mutations(monkeypatch, incomplete):
    payload = complete_candidate_payload(report_day=date(2026, 9, 8))
    if incomplete:
        payload = deepcopy(payload)
        evidence = payload["coverage"]["selected"]["source_range"]
        evidence["state"] = "INCOMPLETE"
        evidence["query_verified"] = False
    mutation_calls = 0

    def forbidden(*_args, **_kwargs):
        nonlocal mutation_calls
        mutation_calls += 1
        raise AssertionError("shadow attempted a forbidden mutation")

    monkeypatch.setattr(repository_v2, "publish_v2_candidate", forbidden)
    monkeypatch.setattr(snapshot, "write_snapshot", forbidden)

    async def builder(_report_day: date) -> CandidateBuildResult:
        return CandidateBuildResult(payload, logical_requests=1, duration_seconds=0.1)

    call = lambda: run_shadow(
        event_schedule=PASS_A,
        started_at=StartedAtObservation(
            utc("2026-09-08T04:51:00Z"),
            StartedAtSource.ACTIONS_RUN_API,
        ),
        candidate_builder=builder,
    )
    if incomplete:
        with pytest.raises(CandidateV2Error):
            call()
    else:
        assert call()["repository"]["write_attempted"] == "NO"
    assert mutation_calls == 0


def test_shadow_hashes_are_deterministic_for_same_candidate():
    payload = complete_candidate_payload(report_day=date(2026, 9, 8))

    async def builder(_report_day: date) -> CandidateBuildResult:
        return CandidateBuildResult(payload, logical_requests=1, duration_seconds=0.1)

    kwargs = {
        "event_schedule": PASS_A,
        "started_at": StartedAtObservation(
            utc("2026-09-08T04:51:00Z"),
            StartedAtSource.ACTIONS_RUN_API,
        ),
        "candidate_builder": builder,
    }
    first = run_shadow(**kwargs)
    second = run_shadow(**kwargs)

    assert first["candidate"] == second["candidate"]


def test_shadow_summary_contains_timing_proof_and_isolation(tmp_path: Path):
    payload = complete_candidate_payload(report_day=date(2026, 9, 8))

    async def builder(_report_day: date) -> CandidateBuildResult:
        return CandidateBuildResult(payload, logical_requests=7, duration_seconds=0.5)

    summary = tmp_path / "summary.md"
    run_shadow(
        event_schedule=PASS_A,
        started_at=StartedAtObservation(
            utc("2026-09-08T06:50:00Z"),
            StartedAtSource.RUNNER_FALLBACK,
            "fixture fallback",
        ),
        summary_path=summary,
        candidate_builder=builder,
    )
    rendered = summary.read_text(encoding="utf-8")

    for required in (
        "event_schedule: `50 4 * * *`",
        "scheduled_for: `2026-09-08T04:50:00Z`",
        "started_at: `2026-09-08T06:50:00Z`",
        "started_at_source: `RUNNER_FALLBACK`",
        "schedule_lag_seconds: `7200`",
        "target_report_date: `2026-09-08`",
        "source_range: `COMPLETE`",
        "state: `VALID_COMPLETE`",
        "Repository WRITE_ATTEMPTED: `NO`",
        "V1: `NOT_TOUCHED`",
        "Consumer: `NOT_TOUCHED`",
        "Pages: `NOT_USED`",
    ):
        assert required in rendered

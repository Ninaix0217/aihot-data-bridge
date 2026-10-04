from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from aihot_bridge import repository_v2, snapshot
from aihot_bridge import shadow_v2
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


def incomplete_observation() -> CandidateBuildResult:
    payload = complete_candidate_payload()
    selected = payload["coverage"]["selected"]
    selected["status"] = "partial"
    selected["source_range"]["state"] = "INCOMPLETE"
    selected["source_range"]["invalid_published_at_items"] = 2
    return CandidateBuildResult(payload, logical_requests=7, duration_seconds=0.5)


def test_failure_diagnostics_preserve_all_channels_without_changing_payload():
    build = incomplete_observation()
    build.payload["unexpected_secret"] = "DO_NOT_LOG"
    before = deepcopy(build.payload)
    observed = shadow_v2._build_diagnostics(build)

    assert observed["validation_state"] == "RAW_OBSERVATION_NOT_VALIDATED"
    assert observed["coverage"]["selected"]["invalid_published_at_items"] == 2
    assert observed["coverage"]["selected"]["source_range_state"] == "INCOMPLETE"
    assert observed["coverage"]["all"]["source_range_state"] == "COMPLETE"
    assert observed["coverage"]["paper"]["source_range_state"] == "COMPLETE"
    assert "DO_NOT_LOG" not in str(observed)
    assert "items" not in observed  # Never dump item bodies or arbitrary metadata.
    assert build.payload == before


def test_failure_diagnostics_handle_malformed_payload_without_masking_error():
    build = CandidateBuildResult({"coverage": None}, 0, 0.1)
    observed = shadow_v2._build_diagnostics(build)
    assert observed["coverage"]["selected"]["source_range_state"] is None


def test_cli_failure_keeps_proof_in_summary_and_log_without_publication(
    monkeypatch, tmp_path, capsys,
):
    summary = tmp_path / "summary.md"
    build = incomplete_observation()
    before = deepcopy(build.payload)
    monkeypatch.setattr(shadow_v2, "parse_args", lambda: SimpleNamespace(
        repository="owner/repo", run_id="123", event_schedule=PASS_A,
        runner_fallback_started_at="2026-09-08T04:51:00Z", summary=summary,
    ))

    def observe(*_args, **_kwargs):
        return StartedAtObservation(
            utc("2026-09-08T04:51:00Z"), StartedAtSource.ACTIONS_RUN_API,
        )

    monkeypatch.setattr(shadow_v2, "observe_started_at", observe)

    async def builder(report_day):
        assert report_day == date(2026, 9, 8)
        return build

    mutation_calls = []
    monkeypatch.setattr(shadow_v2, "build_live_candidate", builder)
    def forbidden(*_args, **_kwargs):
        mutation_calls.append("mutation")
        raise AssertionError("shadow attempted publication")

    monkeypatch.setattr(repository_v2, "publish_v2_candidate", forbidden)
    monkeypatch.setattr(snapshot, "write_snapshot", forbidden)
    assert shadow_v2.main() == 1
    rendered = summary.read_text(encoding="utf-8")
    for required in ("FAIL_CLOSED", "invalid_published_at_items: `2`",
                     "### selected", "### all", "### paper",
                     "RAW_OBSERVATION_NOT_VALIDATED", "Repository WRITE_ATTEMPTED: `NO`"):
        assert required in rendered
    assert rendered.count("## AI HOT V2 Scheduled Shadow") == 1
    logged = capsys.readouterr().err
    assert '"invalid_published_at_items": 2' in logged
    assert "SOURCE_RANGE_INCOMPLETE" in logged
    assert build.payload == before
    assert mutation_calls == []


@pytest.mark.parametrize("build_fails", [False, True])
def test_cli_success_or_prebuild_failure_preserves_existing_contract(
    monkeypatch, tmp_path, capsys, build_fails,
):
    from aihot_bridge.candidate_versioning import artifact_sha256

    summary = tmp_path / "summary.md"
    payload = complete_candidate_payload()
    before_hash = artifact_sha256(payload)
    monkeypatch.setattr(shadow_v2, "parse_args", lambda: SimpleNamespace(
        repository="owner/repo", run_id="123", event_schedule=PASS_A,
        runner_fallback_started_at="2026-09-08T04:51:00Z", summary=summary,
    ))
    monkeypatch.setattr(shadow_v2, "observe_started_at", lambda *args, **kwargs:
        StartedAtObservation(utc("2026-09-08T04:51:00Z"), StartedAtSource.ACTIONS_RUN_API))

    async def builder(_report_day):
        if build_fails:
            raise ValueError("fixture retrieval failed before a payload existed")
        return CandidateBuildResult(payload, logical_requests=1, duration_seconds=0.1)

    monkeypatch.setattr(shadow_v2, "build_live_candidate", builder)
    assert shadow_v2.main() == (1 if build_fails else 0)
    rendered = summary.read_text(encoding="utf-8")
    logged = capsys.readouterr()
    if build_fails:
        assert "FAIL_CLOSED" in rendered
        assert "fixture retrieval failed" in logged.err
        assert "build_observations" not in logged.err
    else:
        assert "VALID_COMPLETE" in rendered
        assert before_hash in logged.out
        assert logged.err == ""
    assert artifact_sha256(payload) == before_hash

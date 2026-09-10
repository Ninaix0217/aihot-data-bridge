from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from aihot_bridge import repository_v2, retrieval_v2
from aihot_bridge.external_relay_v2 import (
    ControlChangeSet,
    GitHubExternalRelayAdapter,
    run_external_relay,
)
from aihot_bridge.external_trigger_v2 import (
    CONTROL_PATH,
    CONTROL_REF,
    ExternalControlError,
    ExternalControlErrorReason,
    ExternalDispatchPlan,
)


NOW = datetime.fromisoformat("2026-09-10T05:00:00+00:00")
BEFORE = "a" * 40
AFTER = "b" * 40


def bootstrap_bytes() -> bytes:
    return json.dumps(
        {
            "schema_version": "aihot-external-trigger/v1",
            "enabled": False,
            "kind": "BOOTSTRAP",
        }
    ).encode()


def active_bytes() -> bytes:
    return json.dumps(
        {
            "schema_version": "aihot-external-trigger/v1",
            "enabled": True,
            "target_report_date": "2026-09-10",
            "mode": "RECOVERY",
            "request_id": "2026-09-10/request-1",
            "requested_at": "2026-09-10T04:59:00Z",
            "source": "external-scheduler",
        }
    ).encode()


@dataclass
class FakeRelayAdapter:
    payload: bytes
    changed_paths: tuple[str, ...] = (CONTROL_PATH,)
    commits: int = 1
    dispatches: list[ExternalDispatchPlan] = field(default_factory=list)

    def read_change_set(self, before: str, after: str) -> ControlChangeSet:
        return ControlChangeSet(before, after, self.changed_paths, self.commits)

    def read_control_payload(self, commit_sha: str) -> bytes:
        assert commit_sha == AFTER
        return self.payload

    def dispatch(self, plan: ExternalDispatchPlan) -> None:
        self.dispatches.append(plan)


def relay(adapter: FakeRelayAdapter, *, actor: str = "Ninaix0217") -> dict:
    return run_external_relay(
        adapter,
        ref=CONTROL_REF,
        before=BEFORE,
        after=AFTER,
        actor=actor,
        actor_id="123",
        now=NOW,
    )


def test_bootstrap_real_shape_is_ignored_without_dispatch():
    adapter = FakeRelayAdapter(bootstrap_bytes())

    result = relay(adapter)

    assert result["trigger_result"] == "IGNORED_BOOTSTRAP"
    assert result["dispatch"] == {"attempted": "NO", "accepted": "NO"}
    assert adapter.dispatches == []


def test_valid_recovery_dispatches_only_fixed_writer_and_ref():
    adapter = FakeRelayAdapter(active_bytes())

    result = relay(adapter)

    assert result["trigger_result"] == "RELAY_ACCEPTED"
    assert result["request_id"] == "2026-09-10/request-1"
    assert len(adapter.dispatches) == 1
    plan = adapter.dispatches[0]
    assert plan.workflow == "v2-rehearsal.yml"
    assert plan.ref == "main"
    assert plan.inputs["mode"] == "RECOVERY"


def test_invalid_payload_scope_or_actor_never_dispatches():
    invalid_payload = json.loads(active_bytes())
    invalid_payload["mode"] = "BACKFILL"
    fixtures = [
        (FakeRelayAdapter(json.dumps(invalid_payload).encode()), "Ninaix0217"),
        (FakeRelayAdapter(active_bytes(), (CONTROL_PATH, "README.md")), "Ninaix0217"),
        (FakeRelayAdapter(active_bytes()), "other-user"),
    ]

    for adapter, actor in fixtures:
        with pytest.raises(ExternalControlError):
            relay(adapter, actor=actor)
        assert adapter.dispatches == []


def test_relay_source_has_no_producer_or_repository_publication_dependency():
    source = (
        Path(__file__).resolve().parents[1]
        / "aihot_bridge"
        / "external_relay_v2.py"
    ).read_text(encoding="utf-8")

    for forbidden in (
        "V2CandidateService",
        "build_live_candidate",
        "repository_v2",
        "github_repository_v2",
        "publish_v2_candidate",
        "UpstreamClient",
        "validate_candidate_v2",
    ):
        assert forbidden not in source


def test_relay_runtime_invokes_neither_producer_nor_repository_publisher(monkeypatch):
    calls = {"producer": 0, "publisher": 0}

    def producer_forbidden(*_args, **_kwargs):
        calls["producer"] += 1
        raise AssertionError("relay invoked Producer")

    def publisher_forbidden(*_args, **_kwargs):
        calls["publisher"] += 1
        raise AssertionError("relay invoked repository publisher")

    monkeypatch.setattr(retrieval_v2, "V2CandidateService", producer_forbidden)
    monkeypatch.setattr(repository_v2, "publish_v2_candidate", publisher_forbidden)

    result = relay(FakeRelayAdapter(active_bytes()))

    assert result["trigger_result"] == "RELAY_ACCEPTED"
    assert calls == {"producer": 0, "publisher": 0}


def test_relay_summary_contains_scope_actor_and_correlation(tmp_path: Path):
    adapter = FakeRelayAdapter(active_bytes())
    summary = tmp_path / "summary.md"

    result = run_external_relay(
        adapter,
        ref=CONTROL_REF,
        before=BEFORE,
        after=AFTER,
        actor="Ninaix0217",
        actor_id="123",
        now=NOW,
        summary_path=summary,
    )
    rendered = summary.read_text(encoding="utf-8")

    assert result["trigger_result"] == "RELAY_ACCEPTED"
    for expected in (
        "TRIGGER_RESULT: `RELAY_ACCEPTED`",
        "actor: `Ninaix0217`",
        f"changed_paths: `{CONTROL_PATH}`",
        "request_id: `2026-09-10/request-1`",
        "fixed workflow: `v2-rehearsal.yml`",
        "fixed ref: `main`",
        "DISPATCH_ACCEPTED: `YES`",
        "RELAY_REPOSITORY_WRITE: `NO`",
        "PRODUCER_EXECUTED_BY_RELAY: `NO`",
    ):
        assert expected in rendered


def git_blob_sha(content: bytes) -> str:
    return hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()


def test_github_adapter_reads_compare_payload_and_dispatches_fixed_request():
    content = active_bytes()
    blob_sha = git_blob_sha(content)
    observed_requests: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read()) if request.content else None
        observed_requests.append((request.method, request.url.path, body))
        if "/compare/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "status": "ahead",
                    "ahead_by": 1,
                    "total_commits": 1,
                    "base_commit": {"sha": BEFORE},
                    "commits": [{"sha": AFTER}],
                    "files": [{"filename": CONTROL_PATH}],
                },
            )
        if "/contents/" in request.url.path:
            assert request.url.params["ref"] == AFTER
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "encoding": "base64",
                    "sha": blob_sha,
                    "size": len(content),
                    "content": base64.b64encode(content).decode(),
                },
            )
        if request.url.path.endswith("/actions/workflows/v2-rehearsal.yml/dispatches"):
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    with httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = GitHubExternalRelayAdapter(client, repository="owner/repo")
        result = run_external_relay(
            adapter,
            ref=CONTROL_REF,
            before=BEFORE,
            after=AFTER,
            actor="Ninaix0217",
            actor_id="123",
            now=NOW,
        )

    assert result["trigger_result"] == "RELAY_ACCEPTED"
    dispatch = observed_requests[-1]
    assert dispatch == (
        "POST",
        "/repos/owner/repo/actions/workflows/v2-rehearsal.yml/dispatches",
        {
            "ref": "main",
            "inputs": {
                "target_report_date": "2026-09-10",
                "mode": "RECOVERY",
                "request_id": "2026-09-10/request-1",
                "trigger_source": "external-scheduler",
            },
        },
    )


@pytest.mark.parametrize("status", [401, 403, 404, 422, 500])
def test_dispatch_http_failure_is_fail_closed(status):
    def handler(request: httpx.Request) -> httpx.Response:
        if "/compare/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "status": "ahead",
                    "ahead_by": 1,
                    "total_commits": 1,
                    "base_commit": {"sha": BEFORE},
                    "commits": [{"sha": AFTER}],
                    "files": [{"filename": CONTROL_PATH}],
                },
            )
        if "/contents/" in request.url.path:
            content = active_bytes()
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "encoding": "base64",
                    "sha": git_blob_sha(content),
                    "size": len(content),
                    "content": base64.b64encode(content).decode(),
                },
            )
        return httpx.Response(status, json={"message": "rejected"})

    with httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = GitHubExternalRelayAdapter(client, repository="owner/repo")
        with pytest.raises(ExternalControlError) as caught:
            run_external_relay(
                adapter,
                ref=CONTROL_REF,
                before=BEFORE,
                after=AFTER,
                actor="Ninaix0217",
                actor_id="123",
                now=NOW,
            )

    assert caught.value.reason is ExternalControlErrorReason.EXTERNAL_DISPATCH_FAILED
    assert caught.value.status_code == status


def test_compare_malformed_or_multi_commit_is_fail_closed():
    responses = [
        {"not": "a compare"},
        {
            "status": "ahead",
            "ahead_by": 2,
            "total_commits": 2,
            "base_commit": {"sha": BEFORE},
            "commits": [{"sha": "c" * 40}, {"sha": AFTER}],
            "files": [{"filename": CONTROL_PATH}],
        },
    ]

    for response in responses:
        with httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(
                lambda _request, response=response: httpx.Response(200, json=response)
            ),
        ) as client:
            adapter = GitHubExternalRelayAdapter(client, repository="owner/repo")
            with pytest.raises(ExternalControlError):
                adapter.read_change_set(BEFORE, AFTER)

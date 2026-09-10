import hashlib
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_workflow(name: str) -> dict:
    path = ROOT / ".github" / "workflows" / name
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_snapshot_schedule_has_only_two_daily_beijing_passes():
    workflow = load_workflow("snapshot-pages.yml")

    schedules = workflow["on"]["schedule"]
    assert [entry["cron"] for entry in schedules] == [
        "50 4 * * *",
        "10 5 * * *",
    ]
    assert "workflow_dispatch" in workflow["on"]


def test_snapshot_concurrency_preserves_running_pass_and_serializes_writes():
    workflow = load_workflow("snapshot-pages.yml")

    assert workflow["concurrency"] == {
        "group": "aihot-daily-producer",
        "cancel-in-progress": "false",
    }
    assert workflow["jobs"]["build"]["permissions"]["contents"] == "write"


def test_producer_summary_is_read_only_non_blocking_observability():
    workflow = load_workflow("snapshot-pages.yml")
    steps = workflow["jobs"]["build"]["steps"]
    names = [step["name"] for step in steps]
    summary = steps[names.index("Write producer run summary")]

    assert names.index("Verify snapshot-data repository object") < names.index(
        "Write producer run summary"
    )
    assert summary["if"] == "always()"
    assert summary["continue-on-error"] == "true"
    assert "aihot_bridge.producer_summary" in summary["run"]
    assert summary["env"]["EVENT_SCHEDULE"] == "${{ github.event.schedule }}"


def test_health_runs_once_daily_after_both_producer_passes():
    workflow = load_workflow("snapshot-health.yml")

    schedules = workflow["on"]["schedule"]
    assert [entry["cron"] for entry in schedules] == ["0 6 * * *"]
    command = workflow["jobs"]["freshness"]["steps"][-1]["run"]
    assert "aihot_bridge.repository_data health" in command
    assert "--branch snapshot-data" in command


def test_v2_rehearsal_is_dispatch_only_with_explicit_inputs():
    workflow = load_workflow("v2-rehearsal.yml")

    assert set(workflow["on"]) == {"workflow_dispatch"}
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["target_report_date"]["required"] == "true"
    assert "default" not in inputs["target_report_date"]
    assert inputs["mode"]["required"] == "true"
    assert inputs["mode"]["options"] == ["MANUAL", "RECOVERY", "BACKFILL"]
    assert inputs["request_id"]["required"] == "false"
    assert inputs["trigger_source"]["required"] == "false"
    assert "default" not in inputs["request_id"]
    assert "default" not in inputs["trigger_source"]


def test_v2_rehearsal_shares_concurrency_but_not_production_workflow():
    workflow = load_workflow("v2-rehearsal.yml")

    assert workflow["concurrency"] == {
        "group": "aihot-daily-producer",
        "cancel-in-progress": "false",
    }
    assert workflow["permissions"] == {"contents": "write"}
    assert set(workflow["jobs"]) == {"rehearsal"}


def test_v2_rehearsal_uses_explicit_dispatch_runner_without_pages():
    workflow = load_workflow("v2-rehearsal.yml")
    steps = workflow["jobs"]["rehearsal"]["steps"]
    command = steps[-1]["run"]

    assert "aihot_bridge.rehearsal_v2" in command
    assert "--report-date" in command
    assert "--mode" in command
    assert "GITHUB_STEP_SUMMARY" in command
    assert "pages" not in workflow["permissions"]


def test_v2_shadow_has_exact_production_schedule_identities_only():
    workflow = load_workflow("v2-shadow.yml")

    assert set(workflow["on"]) == {"schedule"}
    assert [entry["cron"] for entry in workflow["on"]["schedule"]] == [
        "50 4 * * *",
        "10 5 * * *",
    ]


def test_v2_shadow_is_read_only_and_uses_separate_concurrency():
    workflow = load_workflow("v2-shadow.yml")

    assert workflow["concurrency"] == {
        "group": "aihot-v2-shadow",
        "cancel-in-progress": "false",
    }
    assert workflow["concurrency"]["group"] != "aihot-daily-producer"
    assert workflow["permissions"] == {
        "contents": "read",
        "actions": "read",
    }
    assert "pages" not in workflow["permissions"]


def test_v2_shadow_runs_build_only_entrypoint_without_publication_commands():
    workflow = load_workflow("v2-shadow.yml")
    rendered = (ROOT / ".github" / "workflows" / "v2-shadow.yml").read_text(
        encoding="utf-8"
    )
    command = workflow["jobs"]["shadow"]["steps"][-1]["run"]

    assert "aihot_bridge.shadow_v2" in command
    assert "github.event.schedule" in rendered
    for forbidden in (
        "repository_v2",
        "github_repository_v2",
        "publish_v2_candidate",
        "git push",
        "snapshot-data",
        "upload-pages-artifact",
        "deploy-pages",
    ):
        assert forbidden not in rendered


def test_phase_e_does_not_change_v1_or_dispatch_writer_workflows():
    expected = {
        "snapshot-pages.yml": (
            "fa8049a2ccdc28e297b6e64e0f7f8d43d7d03c5ddb20574fb03ce4382560b874"
        ),
        "v2-rehearsal.yml": (
            "0191a0852017fe01a7fda9485b03b7d0aa80617c5c41e31327058e46f4811c19"
        ),
    }

    for filename, expected_sha256 in expected.items():
        content = (ROOT / ".github" / "workflows" / filename).read_bytes()
        content = content.replace(b"\r\n", b"\n")
        assert hashlib.sha256(content).hexdigest() == expected_sha256


def test_external_relay_is_push_only_for_control_branch_and_path():
    workflow = load_workflow("v2-external-relay.yml")

    assert set(workflow["on"]) == {"push"}
    assert workflow["on"]["push"] == {
        "branches": ["aihot-scheduler-control"],
        "paths": [".aihot-control/trigger.json"],
    }


def test_external_relay_permissions_are_read_plus_fixed_dispatch_only():
    workflow = load_workflow("v2-external-relay.yml")

    assert workflow["permissions"] == {
        "contents": "read",
        "actions": "write",
    }
    assert workflow["permissions"].get("contents") != "write"
    for forbidden in ("pages", "id-token", "issues", "pull-requests"):
        assert forbidden not in workflow["permissions"]


def test_external_relay_executes_main_code_but_reads_control_event_by_sha():
    workflow = load_workflow("v2-external-relay.yml")
    steps = workflow["jobs"]["relay"]["steps"]
    checkout = steps[0]
    command = steps[-1]["run"]
    rendered = (
        ROOT / ".github" / "workflows" / "v2-external-relay.yml"
    ).read_text(encoding="utf-8")

    assert checkout["with"] == {"ref": "main", "path": "code"}
    assert "aihot_bridge.external_relay_v2" in command
    assert "github.event.before" in rendered
    assert "github.event.after" in rendered
    assert "github.actor" in rendered
    for forbidden in (
        "schedule:",
        "workflow_dispatch:",
        "repository_v2",
        "publish_v2_candidate",
        "snapshot-data",
        "git push",
    ):
        assert forbidden not in rendered

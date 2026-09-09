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

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


def test_health_runs_once_daily_after_both_producer_passes():
    workflow = load_workflow("snapshot-health.yml")

    schedules = workflow["on"]["schedule"]
    assert [entry["cron"] for entry in schedules] == ["0 6 * * *"]
    command = workflow["jobs"]["freshness"]["steps"][-1]["run"]
    assert "aihot_bridge.repository_data health" in command
    assert "--branch snapshot-data" in command

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_external_scheduler_ci_is_read_only_and_never_deploys():
    path = ROOT / ".github" / "workflows" / "external-scheduler-ci.yml"
    workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert set(workflow["on"]) == {"push", "pull_request"}
    assert workflow["permissions"] == {"contents": "read"}
    rendered = path.read_text(encoding="utf-8")
    assert "external-scheduler/**" in rendered
    assert "npm ci" in rendered
    assert "npm run typecheck" in rendered
    assert "npm test" in rendered
    for forbidden in ("wrangler deploy", "secret put", "contents: write", "schedule:"):
        assert forbidden not in rendered


def test_committed_cloudflare_config_has_no_active_cron_or_secret():
    path = ROOT / "external-scheduler" / "wrangler.jsonc"
    config = json.loads(path.read_text(encoding="utf-8"))

    assert config["triggers"]["crons"] == []
    assert config["vars"] == {"DEPLOYMENT_MODE": "production"}
    rendered = path.read_text(encoding="utf-8")
    assert "GITHUB_APP_PRIVATE_KEY" not in rendered
    assert "05:25" not in rendered
    assert "25 5 * * *" not in rendered

"""The final Mac deploy healthcheck is API-only."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts" / "deploy-native.sh"


def test_final_healthcheck_is_the_stable_api_listener_only() -> None:
    body = DEPLOY.read_text()
    match = re.search(
        r"^run_healthcheck_once\(\) \{\n(.*?)\n\}", body, re.DOTALL | re.MULTILINE
    )
    assert match, "run_healthcheck_once() not found in deploy-native.sh"
    healthcheck = match.group(1)
    assert "http://127.0.0.1:8000/healthz" in healthcheck
    assert "websocket_healthcheck.py" not in healthcheck
    assert "mcp" not in healthcheck.lower()

"""Native deploy rollback keeps the API release coherent."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts" / "deploy-native.sh"


def _function_body(name: str) -> str:
    body = DEPLOY.read_text()
    match = re.search(rf"^{name}\s*\(\)\s*\{{(.*?)^\}}", body, re.MULTILINE | re.DOTALL)
    assert match, f"{name}() function not found"
    return match.group(1)


def test_committed_api_cutover_rolls_back_before_current_symlink() -> None:
    rollback = _function_body("rollback")
    assert "rollback_bluegreen_post_deploy" in rollback
    assert "BLUEGREEN_COMMITTED" in rollback
    assert rollback.index("rollback_bluegreen_post_deploy") < rollback.index(
        'ln -sfn "$PREVIOUS_RELEASE"'
    )


def test_no_single_active_jobs_are_restarted() -> None:
    body = DEPLOY.read_text()
    labels = body.split("SINGLE_ACTIVE_LABELS=(", 1)[1].split(")", 1)[0]
    assert not re.search(r'"com\.robinco\.auto-trader\.', labels)

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
DEPLOY_SCRIPT_PATH = REPO_ROOT / "scripts" / "deploy.sh"


def test_deploy_script_is_retired_fail_closed() -> None:
    """legacy Raspberry Pi Docker deploy script는 실수로 실행되어도 실패해야 한다."""
    content = DEPLOY_SCRIPT_PATH.read_text(encoding="utf-8")

    assert "scripts/deploy.sh is retired" in content
    assert "Raspberry Pi Docker production deploy path was decommissioned" in content
    assert "scripts/deploy-native.sh" in content
    assert "exit 1" in content
    assert 'curl -sf "$HEALTH_URL" > /dev/null 2>&1' not in content
    assert (
        'docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d' not in content
    )


def test_legacy_deploy_workflow_no_longer_ssh_deploys_to_pi() -> None:
    """GHCR workflow는 이미지 빌드만 수행하고 Pi SSH deploy를 수행하지 않는다."""
    content = DEPLOY_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "Build GHCR images" in content
    assert "DEPLOY_SSH_HOST" not in content
    assert "cd /home/mgh3326/auto_trader && ./scripts/deploy.sh" not in content
    assert "scripts/deploy-native.sh" in content

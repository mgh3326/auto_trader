from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
N8N_COMPOSE_PATH = REPO_ROOT / "docker-compose.n8n.yml"
PROD_COMPOSE_PATH = REPO_ROOT / "docker-compose.prod.yml"
DEPLOY_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
DEPLOY_SCRIPT_PATH = REPO_ROOT / "scripts" / "deploy.sh"
README_PATH = REPO_ROOT / "n8n" / "README.md"


def test_n8n_in_separate_compose_file() -> None:
    """n8n은 docker-compose.n8n.yml에 정의되어야 한다."""
    n8n_content = N8N_COMPOSE_PATH.read_text(encoding="utf-8")
    prod_content = PROD_COMPOSE_PATH.read_text(encoding="utf-8")

    # n8n은 별도 compose에 존재
    assert "n8nio/n8n" in n8n_content
    assert "auto_trader_n8n_prod" in n8n_content

    # prod compose에는 n8n 서비스 없음
    assert "n8nio/n8n" not in prod_content


def test_n8n_compose_uses_fixed_internal_port_for_healthcheck() -> None:
    content = N8N_COMPOSE_PATH.read_text(encoding="utf-8")

    assert "N8N_LISTEN_ADDRESS=127.0.0.1" in content
    assert "QUEUE_HEALTH_CHECK_ACTIVE=true" in content
    assert "127.0.0.1:5678/healthz" in content
    assert "N8N_PORT" not in content


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


def test_n8n_readme_documents_fixed_internal_port() -> None:
    content = README_PATH.read_text(encoding="utf-8")

    assert "| `N8N_PORT` |" not in content
    assert "`127.0.0.1:5678`" in content
    assert "curl -f http://127.0.0.1:5678/healthz" in content


def test_n8n_readme_references_separate_compose() -> None:
    content = README_PATH.read_text(encoding="utf-8")

    assert "docker-compose.n8n.yml" in content


def test_deploy_script_has_only_retirement_guidance() -> None:
    """deploy.sh는 legacy prod stack을 시작하지 않고 native 배포 안내만 포함한다."""
    content = DEPLOY_SCRIPT_PATH.read_text(encoding="utf-8")

    assert "docker-compose.n8n.yml" in content  # cleanup guidance only
    assert "logs --tail=50 api" not in content
    assert "deploy-native.sh" in content

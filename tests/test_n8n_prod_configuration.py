from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = REPO_ROOT / "docker-compose.prod.yml"
DEPLOY_SCRIPT_PATH = REPO_ROOT / "scripts" / "deploy.sh"
README_PATH = REPO_ROOT / "n8n" / "README.md"


def test_n8n_compose_uses_fixed_internal_port_for_healthcheck() -> None:
    content = COMPOSE_PATH.read_text(encoding="utf-8")

    assert 'N8N_PORT=${N8N_PORT:-5678}' not in content
    assert 'N8N_LISTEN_ADDRESS=127.0.0.1' in content
    assert 'QUEUE_HEALTH_CHECK_ACTIVE=true' in content
    assert '127.0.0.1:5678/healthz' in content


def test_deploy_script_requires_api_and_n8n_health() -> None:
    content = DEPLOY_SCRIPT_PATH.read_text(encoding="utf-8")

    assert 'N8N_HEALTH_URL=' in content
    assert 'curl -sf "$HEALTH_URL" > /dev/null 2>&1' in content
    assert 'curl -sf "$N8N_HEALTH_URL" > /dev/null 2>&1' in content


def test_n8n_readme_documents_fixed_internal_port() -> None:
    content = README_PATH.read_text(encoding="utf-8")

    assert "| `N8N_PORT` |" not in content
    assert "`127.0.0.1:5678`" in content
    assert "curl -f http://127.0.0.1:5678/healthz" in content

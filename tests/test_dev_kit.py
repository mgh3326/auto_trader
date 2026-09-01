"""Static contracts for the portable, isolated development kit."""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker-compose.dev.yml"
ENV_EXAMPLE_PATH = ROOT / ".env.dev.example"
MAKEFILE_PATH = ROOT / "Makefile"
SEED_SCRIPT_PATH = ROOT / "scripts" / "make_dev_seed.py"
HEALTH_SCRIPT_PATH = ROOT / "scripts" / "devkit_healthcheck.py"
RUNBOOK_PATH = ROOT / "docs" / "runbooks" / "dev-kit.md"

PLACEHOLDER_PATTERN = re.compile(r"DEV_PLACEHOLDER_[A-Z0-9_]+\Z")
REALISH_SECRET_PATTERN = re.compile(r"[A-Za-z0-9_+=/-]{24,}\Z")
AWS_ACCESS_KEY_PATTERN = re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}\Z")

BROKER_CREDENTIAL_KEYS = frozenset(
    {
        "KIS_APP_KEY",
        "KIS_APP_SECRET",
        "KIS_ACCESS_TOKEN",
        "KIS_ACCOUNT_NO",
        "KIS_WS_HTS_ID",
        "KIS_MOCK_APP_KEY",
        "KIS_MOCK_APP_SECRET",
        "KIS_MOCK_ACCESS_TOKEN",
        "KIS_MOCK_ACCOUNT_NO",
        "KIWOOM_MOCK_APP_KEY",
        "KIWOOM_MOCK_APP_SECRET",
        "KIWOOM_MOCK_ACCESS_TOKEN",
        "KIWOOM_MOCK_ACCOUNT_NO",
        "KIWOOM_MOCK_US_APP_KEY",
        "KIWOOM_MOCK_US_APP_SECRET",
        "KIWOOM_MOCK_US_ACCOUNT_NO",
        "KIWOOM_LIVE_APP_KEY",
        "KIWOOM_LIVE_APP_SECRET",
        "UPBIT_ACCESS_KEY",
        "UPBIT_SECRET_KEY",
        "ALPACA_PAPER_API_KEY",
        "ALPACA_PAPER_API_SECRET",
        "ALPACA_PAPER_LAB_API_KEY",
        "ALPACA_PAPER_LAB_API_SECRET",
        "ALPACA_DATA_API_KEY_ID",
        "ALPACA_DATA_API_SECRET_KEY",
        "BINANCE_DEMO_API_KEY",
        "BINANCE_DEMO_API_SECRET",
        "BINANCE_SPOT_DEMO_API_KEY",
        "BINANCE_SPOT_DEMO_API_SECRET",
        "BINANCE_FUTURES_DEMO_API_KEY",
        "BINANCE_FUTURES_DEMO_API_SECRET",
        "TOSS_API_CLIENT_ID",
        "TOSS_API_CLIENT_SECRET",
        "NHPLUG_APP_KEY",
        "NHPLUG_APP_SECRET",
        "NHPLUG_MOCK_ACCOUNT_NO",
        "NHPLUG_LIVE_APP_KEY",
        "NHPLUG_LIVE_APP_SECRET",
    }
)


def _parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        values[key] = value
    return values


def _realish_credential_violations(text: str) -> list[str]:
    values = _parse_env(text)
    violations: list[str] = []
    for key in BROKER_CREDENTIAL_KEYS:
        value = values.get(key, "")
        if PLACEHOLDER_PATTERN.fullmatch(value):
            continue
        if AWS_ACCESS_KEY_PATTERN.fullmatch(value) or REALISH_SECRET_PATTERN.fullmatch(
            value
        ):
            violations.append(key)
    return sorted(violations)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_dev_compose_uses_parameterized_loopback_ports_and_no_global_names() -> None:
    compose = yaml.safe_load(_read(COMPOSE_PATH))

    assert compose["name"] == "${COMPOSE_PROJECT_NAME:-at-dev-local}"
    assert compose["services"]["db"]["image"] == "timescale/timescaledb:2.22.1-pg17"
    assert compose["services"]["db"]["ports"] == [
        "${DEV_PG_BIND_HOST:-127.0.0.1}:${DEV_PG_PORT:-55432}:5432"
    ]
    assert compose["services"]["redis"]["ports"] == [
        "${DEV_REDIS_BIND_HOST:-127.0.0.1}:${DEV_REDIS_PORT:-56379}:6379"
    ]

    for service_name in ("db", "redis"):
        service = compose["services"][service_name]
        assert "container_name" not in service
        assert service["restart"] == "no"
        assert service["mem_limit"]

    assert compose["volumes"] == {"postgres_data": None, "redis_data": None}
    assert compose["networks"]["devnet"] == {"driver": "bridge"}


def test_dev_compose_manifest_witness_contract_is_documented() -> None:
    runbook = _read(RUNBOOK_PATH)
    assert (
        "docker buildx imagetools inspect timescale/timescaledb:2.22.1-pg17" in runbook
    )
    assert "linux/amd64" in runbook
    assert "linux/arm64" in runbook
    assert "2.26.3" in runbook


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is unavailable")
def test_dev_compose_config_is_valid_when_docker_cli_is_available() -> None:
    environment = dict(os.environ)
    environment.update(
        {
            "COMPOSE_PROJECT_NAME": "at-dev-config-test",
            "DEV_PG_PORT": "55439",
            "DEV_REDIS_PORT": "56389",
        }
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ENV_EXAMPLE_PATH),
            "-f",
            str(COMPOSE_PATH),
            "config",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "55439:5432" in result.stdout
    assert "56389:6379" in result.stdout


def test_make_dry_runs_keep_two_dev_projects_and_port_pairs_disjoint(
    tmp_path: Path,
) -> None:
    first_env = tmp_path / "first.env"
    first_env.write_text(
        "COMPOSE_PROJECT_NAME=at-dev-alpha\nDEV_PG_PORT=55432\nDEV_REDIS_PORT=56379\n",
        encoding="utf-8",
    )
    second_env = tmp_path / "second.env"
    second_env.write_text(
        "COMPOSE_PROJECT_NAME=at-dev-beta\nDEV_PG_PORT=55433\nDEV_REDIS_PORT=56380\n",
        encoding="utf-8",
    )

    def dry_run(env_file: Path) -> str:
        result = subprocess.run(
            ["make", "-n", f"DEV_ENV_FILE={env_file}", "dev-up"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    first = dry_run(first_env)
    second = dry_run(second_env)

    assert 'COMPOSE_PROJECT_NAME="at-dev-alpha"' in first
    assert 'DEV_PG_PORT="55432"' in first
    assert 'DEV_REDIS_PORT="56379"' in first
    assert 'COMPOSE_PROJECT_NAME="at-dev-beta"' in second
    assert 'DEV_PG_PORT="55433"' in second
    assert 'DEV_REDIS_PORT="56380"' in second
    assert first != second


def test_dev_example_has_fixed_broker_placeholders_and_all_gates_off() -> None:
    values = _parse_env(_read(ENV_EXAMPLE_PATH))

    missing = sorted(BROKER_CREDENTIAL_KEYS - values.keys())
    assert not missing
    invalid = {
        key: values[key]
        for key in BROKER_CREDENTIAL_KEYS
        if not PLACEHOLDER_PATTERN.fullmatch(values[key])
    }
    assert not invalid

    gates = {key: value for key, value in values.items() if key.endswith("_ENABLED")}
    assert gates
    assert all(value.lower() == "false" for value in gates.values())


def test_dev_example_real_key_pattern_mutant_is_rejected() -> None:
    text = _read(ENV_EXAMPLE_PATH)
    mutant = text.replace(
        "KIS_APP_KEY=DEV_PLACEHOLDER_KIS_APP_KEY",
        "KIS_APP_KEY=AKIAABCDEFGHIJKLMNOP",
    )

    assert "KIS_APP_KEY" in _realish_credential_violations(mutant)


def test_dev_example_loads_in_a_minimal_runtime_environment() -> None:
    environment = {
        "PATH": os.environ["PATH"],
        "TMPDIR": "/tmp",
        "ENV_FILE": str(ENV_EXAMPLE_PATH),
        "DATABASE_URL": "postgresql+asyncpg://atdev:atdev-local-password@127.0.0.1:55432/auto_trader_dev",
        "REDIS_URL": "redis://127.0.0.1:56379/0",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from app.core.config import settings; "
                "assert settings.kis_mock_enabled is False; "
                "assert settings.toss_api_enabled is False"
            ),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_seed_exporter_source_sql_is_select_and_copy_out_only() -> None:
    source = _read(SEED_SCRIPT_PATH)
    tree = ast.parse(source)
    query_literals: list[str] = []
    source_write_methods: list[str] = []
    has_copy_out = False
    has_readonly_transaction = False

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = {
                target.id for target in node.targets if isinstance(target, ast.Name)
            }
            if any(name.endswith("_QUERY") for name in names):
                assert isinstance(node.value, ast.Constant)
                assert isinstance(node.value.value, str)
                query_literals.append(node.value.value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            method_name = node.func.attr
            if method_name in {"execute", "executemany"}:
                source_write_methods.append(method_name)
            if method_name == "copy_from_query":
                has_copy_out = True
            if method_name == "transaction" and any(
                keyword.arg == "readonly"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            ):
                has_readonly_transaction = True

    assert query_literals
    assert all(query.lstrip().upper().startswith("SELECT") for query in query_literals)
    assert not source_write_methods
    assert has_copy_out
    assert has_readonly_transaction
    assert 'isolation="serializable"' in source


def test_make_targets_keep_dev_workflow_bounded_and_local() -> None:
    makefile = _read(MAKEFILE_PATH)

    assert "dev-up: _dev-env-check" in makefile
    assert "pg_isready" in makefile
    assert "DEV_UP_TIMEOUT_SECONDS" in makefile
    assert "dev-seed: dev-up" in makefile
    assert "uv run alembic upgrade head" in makefile
    assert "No $(DEV_SEED_DUMP) found" in makefile
    assert "dev-verify: dev-up" in makefile
    assert "uv run alembic current" in makefile
    assert "uv run pytest tests/test_dev_kit.py -q --no-cov" in makefile
    assert "uv run uvicorn app.main:app" in makefile
    assert 'DEV_RUNTIME_SANITIZER = env -i PATH="$(PATH)" TMPDIR=/tmp' in makefile
    assert (
        'DEV_COMPOSE_ENV = COMPOSE_PROJECT_NAME="$(COMPOSE_PROJECT_NAME)"' in makefile
    )
    assert "dev-down: _dev-env-check" in makefile
    assert "down --remove-orphans" in makefile


def test_health_probe_is_fixed_to_loopback() -> None:
    source = _read(HEALTH_SCRIPT_PATH)

    assert 'LOOPBACK_HOST = "127.0.0.1"' in source
    assert 'HEALTH_PATH = "/healthz"' in source
    assert "requests" not in source

"""Static guards: the Dagu proposal runs only the new commands, and the
package reaches no database, broker, credential or scheduler code."""

from __future__ import annotations

import ast
import shlex
from pathlib import Path

import pytest
import yaml

from research.data_plane.cli import build_parser
from research.data_plane.public_http import DisallowedUrlError, check_url

REPO = Path(__file__).resolve().parents[3]
DAG_DIR = REPO / "deploy" / "dagu" / "data-plane"
PACKAGE = REPO / "research" / "data_plane"
COMMAND_PREFIX = ["uv", "run", "python", "-m", "research.data_plane.cli"]
DAG_FILES = sorted(DAG_DIR.glob("*.yaml"))


@pytest.mark.unit
def test_dag_proposals_exist_one_per_market():
    assert [p.name for p in DAG_FILES] == [
        "data-plane-crypto.yaml",
        "data-plane-kr.yaml",
        "data-plane-us.yaml",
    ]


@pytest.mark.unit
@pytest.mark.parametrize("path", DAG_FILES, ids=lambda p: p.name)
def test_dag_proposal_is_valid_yaml_running_only_data_plane_commands(path):
    doc = yaml.safe_load(path.read_text())
    assert isinstance(doc, dict)
    # Dagu >= 2.x rejects an entrypoint ``name`` and camelCase keys.
    assert "name" not in doc
    market = path.stem.removeprefix("data-plane-")
    steps = doc["steps"]
    assert steps and isinstance(steps, list)
    names = {s["name"] for s in steps}
    parser = build_parser()
    for step in steps:
        assert set(step) <= {
            "name",
            "working_dir",
            "command",
            "timeout_sec",
            "retry_policy",
            "continue_on",
            "depends",
        }
        assert set(step.get("depends", [])) <= names
        argv = shlex.split(step["command"])
        assert argv[: len(COMMAND_PREFIX)] == COMMAND_PREFIX, step["command"]
        args = parser.parse_args(argv[len(COMMAND_PREFIX) :])  # flags must exist
        assert args.market == market
    commands = [shlex.split(s["command"])[len(COMMAND_PREFIX)] for s in steps]
    assert commands[0] == "collect"
    datasets = {
        parser.parse_args(shlex.split(s["command"])[5:]).dataset for s in steps[1:]
    }
    assert datasets == {"universe", "bars_1d", "corporate_actions"}


FORBIDDEN_IMPORT_PREFIXES = (
    "app",
    "sqlalchemy",
    "asyncpg",
    "psycopg",
    "redis",
    "taskiq",
    "prefect",
    "httpx",
)
ALLOWED_APP_IMPORTS = {"app.core.symbol"}


@pytest.mark.unit
def test_package_imports_no_db_broker_or_scheduler_code():
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                if name in ALLOWED_APP_IMPORTS:
                    continue
                root = name.split(".")[0]
                assert root not in FORBIDDEN_IMPORT_PREFIXES, (
                    f"{path.name} imports {name}"
                )


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "https://api.upbit.com/v1/orders",
        "https://api.binance.com/api/v3/order",
        "https://data-api.binance.vision/api/v3/account",
        "https://query2.finance.yahoo.com/v7/finance/quote?symbols=AAPL",
        "http://query2.finance.yahoo.com/v8/finance/chart/AAPL",
        "https://user:pw@api.upbit.com/v1/market/all",
        "https://query2.finance.yahoo.com/v8/finance/chart/..%2Fv7",
    ],
)
def test_fetcher_refuses_non_market_data_urls(url):
    with pytest.raises(DisallowedUrlError):
        check_url(url)


@pytest.mark.unit
def test_kis_master_sources_match_kr_delist_repair():
    from research.data_plane import sources
    from research.kr_delist_repair import kind_fetch

    assert sources.KIS_MASTER == kind_fetch.KIS_MASTER
    assert sources.KIS_MASTER_FILES == kind_fetch.KIS_MASTER_FILES


@pytest.mark.unit
def test_importing_the_cli_loads_no_db_or_scheduler_module():
    import subprocess
    import sys

    probe = (
        "import sys, research.data_plane.cli; "
        "bad=[m for m in ('httpx','sqlalchemy','asyncpg','psycopg','redis','taskiq','prefect') "
        "if m in sys.modules]; print(','.join(bad))"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == ""

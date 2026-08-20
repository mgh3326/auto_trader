"""ROB-1303 is observation-only. This asserts it, rather than trusting it.

The package and its two entry points (CLI, MCP tool) must not import a broker,
an order/approval/watch path, or a scheduler, and must not contain a write
statement of their own.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
PACKAGE = REPO / "app/services/spike_attribution"
ENTRY_POINTS = (
    REPO / "scripts/attribute_daily_spikes.py",
    REPO / "scripts/precompute_spike_attribution.py",
    REPO / "app/mcp_server/tooling/spike_attribution.py",
    REPO / "app/mcp_server/tooling/spike_attribution_registration.py",
)

FORBIDDEN_IMPORT_FRAGMENTS = (
    "app.services.brokers",
    "app.services.kis_trading_service",
    "app.services.order",
    "app.mcp_server.tooling.orders",
    "app.core.scheduler",
    "app.core.taskiq_broker",
    "taskiq",
    "prefect",
)

FORBIDDEN_SQL_TOKENS = ("INSERT ", "UPDATE ", "DELETE ", "UPSERT ", "TRUNCATE ")


def python_files() -> list[pathlib.Path]:
    return sorted(PACKAGE.glob("*.py")) + list(ENTRY_POINTS)


def imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("path", python_files(), ids=lambda p: p.name)
def test_no_broker_order_or_scheduler_import(path: pathlib.Path) -> None:
    for module in imported_modules(path):
        for fragment in FORBIDDEN_IMPORT_FRAGMENTS:
            assert not module.startswith(fragment), f"{path.name} imports {module}"


@pytest.mark.parametrize("path", python_files(), ids=lambda p: p.name)
def test_no_write_sql(path: pathlib.Path) -> None:
    source = path.read_text().upper()
    for token in FORBIDDEN_SQL_TOKENS:
        assert token not in source, f"{path.name} contains {token.strip()}"


@pytest.mark.parametrize("path", python_files(), ids=lambda p: p.name)
def test_no_orm_session_write_calls(path: pathlib.Path) -> None:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {
            "add",
            "add_all",
            "commit",
            "flush",
            "merge",
        }:
            value = node.value
            if isinstance(value, ast.Name) and value.id in {"db", "session"}:
                pytest.fail(f"{path.name} calls {value.id}.{node.attr}")


def test_the_cli_never_calls_forecast_save() -> None:
    source = (REPO / "scripts/attribute_daily_spikes.py").read_text()
    assert "forecast_save(" not in source
    assert "build_prereg_forecasts" in source


def test_the_mcp_tool_never_calls_forecast_save() -> None:
    source = (REPO / "app/mcp_server/tooling/spike_attribution.py").read_text()
    assert "forecast_save(" not in source


def test_the_tool_is_registered_on_the_read_only_profiles() -> None:
    from app.mcp_server.tooling.analysis_readonly_registration import (
        ANALYSIS_READONLY_FORBIDDEN_TOOL_NAMES,
        ANALYSIS_READONLY_TOOL_NAMES,
    )
    from app.mcp_server.tooling.analysis_registration import ANALYSIS_TOOL_NAMES
    from app.mcp_server.tooling.spike_attribution_registration import (
        SPIKE_ATTRIBUTION_TOOL_NAMES,
    )

    assert SPIKE_ATTRIBUTION_TOOL_NAMES == {"get_spike_attribution"}
    assert SPIKE_ATTRIBUTION_TOOL_NAMES <= ANALYSIS_TOOL_NAMES
    assert SPIKE_ATTRIBUTION_TOOL_NAMES <= ANALYSIS_READONLY_TOOL_NAMES
    assert not SPIKE_ATTRIBUTION_TOOL_NAMES & ANALYSIS_READONLY_FORBIDDEN_TOOL_NAMES


def test_the_package_declares_no_scheduler_registration() -> None:
    for path in REPO.glob("app/flows/*.py"):
        assert "spike_attribution" not in path.read_text(), path.name
    for path in REPO.glob("app/tasks/**/*.py"):
        assert "spike_attribution" not in path.read_text(), path.name


def test_the_precompute_cli_registers_no_cadence_of_its_own() -> None:
    """--mode only stamps a freshness yardstick; it must not schedule anything."""

    source = (REPO / "scripts/precompute_spike_attribution.py").read_text()
    for token in ("crontab", "CronCreate", "schedule(", "add_job", "@broker.task"):
        assert token not in source, token
    assert "No schedule is registered" in source


def test_the_cache_writes_files_not_database_rows() -> None:
    import datetime as dt

    from app.services.spike_attribution.precompute import PrecomputeRun

    run = PrecomputeRun(
        market="kr",
        session_date=dt.date(2026, 8, 20),
        mode="preopen",
        started_at=dt.datetime(2026, 8, 20, tzinfo=dt.UTC),
        finished_at=dt.datetime(2026, 8, 20, tzinfo=dt.UTC),
    )
    payload = run.as_dict()
    assert payload["db_rows_written"] == 0
    assert payload["scheduler_registration"] is False
    assert payload["promote"] is False

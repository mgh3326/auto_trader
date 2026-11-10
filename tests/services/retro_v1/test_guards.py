"""retro/v1 readers are read-only by construction: import, call and SQL guards."""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest

from app.services.retro_v1 import (
    RetroReadError,
    read_data_gaps,
    read_get_bars,
    read_mock_experiments,
    read_retro_day,
    read_retro_funnel,
    read_retro_positions,
    read_retro_trades,
    read_retro_symbol,
)
from app.services.retro_v1.envelope import dec, read_snapshot
from tests.services.retro_v1.support import FakeSession, kr_day_session, validate
from tests.services.retro_v1.test_other_readers import gap_session

pytestmark = pytest.mark.unit

PACKAGE = Path(__file__).resolve().parents[3] / "app/services/retro_v1"
#: Direct imports a retro/v1 module must never have: broker clients, order and
#: proposal machinery, the paper-strategy recorder, reconcile/refresh jobs, MCP,
#: routers and every scheduler/worker surface.
FORBIDDEN_IMPORTS = (
    "app.services.brokers",
    "app.services.order_proposals",
    "app.services.order_",
    "app.services.mock_strategy",
    "app.services.kis_",
    "app.services.upbit",
    "app.services.toss",
    "app.services.account_value",
    "app.services.execution_ledger.reconcile",
    "app.services.execution_ledger.quarantine",
    "app.services.symbol_sectors_service",
    "app.mcp_server",
    "app.routers",
    "app.tasks",
    "app.flows",
    "app.jobs",
    "taskiq",
    "prefect",
)
#: Anything in the transitive closure of importing the package that would mean a
#: broker, router, worker or MCP surface got loaded.
FORBIDDEN_LOADED = (
    "app.services.brokers",
    "app.mcp_server",
    "app.routers",
    "app.tasks",
    "app.flows",
    "app.jobs",
    "app.services.mock_strategy",
    "taskiq",
    "prefect",
)
#: Calls that write or end a transaction. ``begin``/``begin_nested`` open the
#: reader's own read-only transaction and are not in this list.
FORBIDDEN_CALLS = {
    "commit",
    "rollback",
    "add",
    "add_all",
    "flush",
    "delete",
    "merge",
    "bulk_save_objects",
    "upsert_fill",
    "upsert",
    "mark_quarantined",
    "append_quarantine_events",
    "record_run",
    "refresh",
    "expire",
}


def modules() -> list[Path]:
    files = sorted(PACKAGE.glob("*.py"))
    assert files
    return files


def imported_names(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            names += [f"{node.module}.{alias.name}" for alias in node.names]
    return names


def test_no_retro_module_imports_a_broker_order_writer_or_scheduler() -> None:
    offenders = [
        f"{path.name}: {name}"
        for path in modules()
        for name in imported_names(path)
        if name.startswith(FORBIDDEN_IMPORTS)
    ]
    assert offenders == []


def static_closure(roots: list[Path]) -> set[str]:
    """Every ``app.*`` module reachable by an import statement, lazy ones included.

    Static on purpose: it also counts imports inside functions, which a runtime
    ``sys.modules`` check after importing the package would miss.
    """
    root = PACKAGE.parents[2]
    seen: set[str] = set()
    pending = list(roots)
    while pending:
        path = pending.pop()
        for name in imported_names(path):
            if not name.startswith("app."):
                continue
            parts = name.split(".")
            # ``from a.b import c`` yields both ``a.b`` and ``a.b.c``: resolve the
            # longest prefix that is a module or package on disk.
            for end in range(len(parts), 0, -1):
                base = root.joinpath(*parts[:end])
                target = base.with_suffix(".py")
                if base.is_dir() and (base / "__init__.py").exists():
                    target = base / "__init__.py"
                if target.exists():
                    module = ".".join(parts[:end])
                    if module not in seen:
                        seen.add(module)
                        pending.append(target)
                    break
    return seen


def test_the_transitive_import_closure_reaches_no_broker_router_mcp_or_worker_module() -> (
    None
):
    closure = static_closure(modules())
    assert any(m.startswith("app.models") for m in closure)  # the walk really ran
    offenders = sorted(m for m in closure if m.startswith(FORBIDDEN_LOADED))
    assert offenders == []


def test_no_retro_module_calls_a_write_or_transaction_ending_method() -> None:
    offenders = []
    for path in modules():
        for node in ast.walk(ast.parse(path.read_text())):
            if not (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            ):
                continue
            receiver = ast.unparse(node.func.value)
            on_session = receiver.startswith(("session", "db", "conn", "repo"))
            # ``set.add`` is fine; ``session.add`` is not.
            if node.func.attr in FORBIDDEN_CALLS and (
                node.func.attr not in {"add", "add_all"} or on_session
            ):
                offenders.append(
                    f"{path.name}:{node.lineno} {receiver}.{node.func.attr}()"
                )
    assert offenders == []


def test_the_scoreboard_reader_is_not_part_of_this_package() -> None:
    assert not (PACKAGE / "retro_scoreboard.py").exists()
    assert not (PACKAGE.parents[0] / "account_value" / "retro_v1.py").exists()


@pytest.mark.asyncio
async def test_every_reader_runs_in_a_read_only_repeatable_read_snapshot_with_selects_only() -> (
    None
):
    day = date(2026, 1, 5)
    calls = [
        (read_retro_day, {"market": "kr", "date": day}, kr_day_session),
        (
            read_retro_symbol,
            {"symbol": "005930", "from_": day, "to": day},
            kr_day_session,
        ),
        (read_retro_funnel, {"market": "kr", "from_": day, "to": day}, kr_day_session),
        (read_retro_positions, {"cutoff_date": day}, kr_day_session),
        (read_retro_trades, {"from_": day, "to": day}, FakeSession),
        (read_mock_experiments, {}, FakeSession),
        (
            read_get_bars,
            {"symbol": "005930", "tf": "1d", "from_": day, "to": day},
            kr_day_session,
        ),
        (read_data_gaps, {}, gap_session),
    ]
    assert len(calls) == 8
    for reader, kwargs, make in calls:
        session = make()
        response = await reader(session, **kwargs)
        assert response["as_of"] and "data" in response
        first, second, *rest = session.statements
        assert first.startswith(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
        )
        assert "statement_timeout" in second
        assert session.begun == 1  # one snapshot for the count and the rows
        for statement in rest:
            assert statement.lstrip().upper().startswith(("SELECT", "WITH")), statement


@pytest.mark.asyncio
async def test_a_session_already_in_a_transaction_is_refused_before_any_statement() -> (
    None
):
    session = FakeSession()
    session._in_tx = True
    with pytest.raises(RetroReadError) as err:
        async with read_snapshot(session):  # type: ignore[arg-type]
            raise AssertionError("must not enter")
    assert err.value.code == "validation_error"
    assert session.statements == []


def test_money_is_exact_decimal_text_and_a_float_is_refused() -> None:
    from decimal import Decimal

    assert dec(Decimal("9007199254740993.0000")) == "9007199254740993.0000"
    assert dec(Decimal("1E+3")) == "1000"
    assert dec("70000") == "70000"
    assert dec(None) is None
    with pytest.raises(TypeError):
        dec(0.1)


@pytest.mark.asyncio
async def test_a_response_missing_as_of_or_with_a_float_money_value_fails_the_schema() -> (
    None
):
    import copy

    import jsonschema

    response = await read_retro_day(
        kr_day_session(), market="kr", date=date(2026, 1, 5)
    )
    validate(response, "RetroDayResponse")
    no_as_of = copy.deepcopy(response)
    del no_as_of["as_of"]
    with pytest.raises(jsonschema.ValidationError):
        validate(no_as_of, "RetroDayResponse")
    float_money = copy.deepcopy(response)
    float_money["data"][-1]["quantity"] = 3.0
    with pytest.raises(jsonschema.ValidationError):
        validate(float_money, "RetroDayResponse")

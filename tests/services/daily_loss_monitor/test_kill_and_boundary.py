"""hk 7119 — writer, kill read, migration shape and import boundary (no DB)."""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

from app.services.daily_loss_monitor import monitor, writer
from app.services.daily_loss_monitor.accounts import ACCOUNTS
from app.services.daily_loss_monitor.reader import AccountFills
from app.services.daily_loss_monitor.rules import Fill, Mark

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "alembic/versions/20261128_hk7119_daily_loss_freeze.py"
NOW = datetime(2026, 11, 28, 5, 0, tzinfo=UTC)


def _migration():
    spec = importlib.util.spec_from_file_location("hk7119_mig", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Session:
    def __init__(self, returned=None):
        self.statements = []
        self.returned = returned
        self.commits = 0

    async def execute(self, statement, *a, **k):
        self.statements.append(statement)
        returned = self.returned

        class _R:
            def scalar_one_or_none(self):
                return returned

        return _R()

    async def rollback(self):
        pass

    async def commit(self):
        self.commits += 1


def _sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


@pytest.mark.asyncio
async def test_writer_only_moves_active_to_daily_loss_freeze() -> None:
    session = _Session(returned=None)
    moved = await writer.freeze_if_active(session, account="upbit_live", reason="x")
    assert moved is False  # a non-ACTIVE row is untouched
    sql = _sql(session.statements[0])
    assert "WHERE review.execution_account_control.account = 'upbit_live'" in sql
    assert "execution_account_control.mode = 'ACTIVE'" in sql
    assert "mode='DAILY_LOSS_FREEZE'" in sql
    assert "updated_by='system:daily_loss_monitor'" in sql


def test_writer_never_writes_active() -> None:
    source = (ROOT / "app/services/daily_loss_monitor/writer.py").read_text()
    tree = ast.parse(source)
    values = [
        kw.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "values"
        for kw in node.keywords
        if kw.arg == "mode"
    ]
    assert len(values) == 1 and isinstance(values[0], ast.Name)
    assert values[0].id == "DAILY_LOSS_FREEZE_MODE"


def test_migration_shape() -> None:
    m = _migration()
    assert m.down_revision == "20261128_hk6409_backfill_req"
    assert len(m.revision) <= 32
    assert set(m.NEW_ACCOUNTS) | {"upbit_live"} == set(ACCOUNTS)
    seeds = [s for s in m.UPGRADE_DDL if s.startswith("INSERT")]
    assert len(seeds) == 4
    for account, seed in zip(m.NEW_ACCOUNTS, seeds, strict=True):
        assert f"'{account}', 'GLOBAL_FREEZE'" in seed
        assert "ON CONFLICT (account) DO NOTHING" in seed
    joined = "\n".join(m.UPGRADE_DDL)
    assert "'DAILY_LOSS_FREEZE'" in m.MODE_CHECK
    assert "OLD.mode <> 'ACTIVE'" in joined
    assert "system:daily_loss_monitor" in joined
    assert "pg_roles WHERE rolname = 'at_app'" in m.GRANT_DDL[0]
    assert "DELETE" not in m.GRANT_DDL[0]
    down = ast.get_source_segment(
        MIGRATION.read_text(),
        next(
            n
            for n in ast.parse(MIGRATION.read_text()).body
            if isinstance(n, ast.FunctionDef) and n.name == "downgrade"
        ),
    )
    assert "raise RuntimeError" in down


def test_model_checks_match_migration() -> None:
    from app.models.execution_mandates import ExecutionAccountControl

    m = _migration()
    checks = {
        c.name: str(c.sqltext)
        for c in ExecutionAccountControl.__table__.constraints
        if hasattr(c, "sqltext")
    }
    assert m.ACCOUNT_CHECK in checks.values()
    assert m.MODE_CHECK in checks.values()


@pytest.mark.asyncio
async def test_missing_row_and_daily_loss_freeze_read_frozen() -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from app.services.execution_intent_gateway.account_control import (
        PostgresAccountControlStore,
    )
    from app.services.kis_mock_runner.control import (
        KillMode,
        read_effective_kill_switch,
    )

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        execution_options={"schema_translate_map": {"review": None}},
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE execution_account_control (account TEXT PRIMARY KEY,"
                    " mode TEXT, reason TEXT, updated_by TEXT, updated_at TIMESTAMP)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO execution_account_control VALUES ('kis_live_kr',"
                    " 'DAILY_LOSS_FREEZE', 'daily_loss', 'system:daily_loss_monitor',"
                    " '2026-11-28 00:00:00')"
                )
            )
        sessions = async_sessionmaker(engine, class_=AsyncSession)
        frozen = await read_effective_kill_switch(
            PostgresAccountControlStore("kis_live_kr", sessions)
        )
        assert frozen.mode is KillMode.GLOBAL_FREEZE
        assert frozen.reason.startswith("DAILY_LOSS_FREEZE")
        missing = await read_effective_kill_switch(
            PostgresAccountControlStore("toss_live_us", sessions)
        )
        assert missing.mode is KillMode.GLOBAL_FREEZE and missing.read_failed
    finally:
        await engine.dispose()


def _fills(history, today, provisional=frozenset()):
    async def load(session, scope, *, day_start):
        return AccountFills(history, today, provisional)

    return load


async def _marks(price, age=timedelta(minutes=1)):
    async def fn(session, scope, symbols, now):
        return {s: Mark(Decimal(price), now - age, None) for s in symbols}

    return fn


@pytest.mark.asyncio
async def test_run_once_freezes_on_loss_and_previews_without_writing(
    monkeypatch,
) -> None:
    held = Fill(
        "005930",
        "buy",
        Decimal("100"),
        Decimal("70000"),
        Decimal("0"),
        None,
        "KRW",
        "KRW",
        NOW - timedelta(days=2),
        None,
    )
    monkeypatch.setattr(monitor, "load_account_fills", _fills([held], []))

    async def fx():
        return Decimal("1400")

    ref = await _marks("60000")
    quote = await _marks("54000")
    preview = _Session(returned="kis_live_kr")
    out = await monitor.run_once(
        preview,
        now=NOW,
        reference_fn=ref,
        quote_fn=quote,
        fx_fn=fx,
        commit=False,
        accounts=["kis_live_kr"],
    )
    assert out[0].freeze and not out[0].frozen_now
    assert preview.statements == [] and preview.commits == 0

    committed = _Session(returned="kis_live_kr")
    out = await monitor.run_once(
        committed,
        now=NOW,
        reference_fn=ref,
        quote_fn=quote,
        fx_fn=fx,
        commit=True,
        accounts=["kis_live_kr"],
    )
    assert out[0].pnl_krw == Decimal("-600000") and out[0].frozen_now
    assert committed.commits == 1


@pytest.mark.asyncio
async def test_unreadable_ledger_freezes(monkeypatch) -> None:
    async def boom(session, scope, *, day_start):
        raise RuntimeError("db down")

    monkeypatch.setattr(monitor, "load_account_fills", boom)
    ref = await _marks("1")
    out = await monitor.run_once(
        _Session(),
        now=NOW,
        reference_fn=ref,
        quote_fn=ref,
        fx_fn=None,
        commit=False,
        accounts=["upbit_live"],
    )
    assert out[0].reasons == ("ledger_unreadable",) and out[0].freeze


_FORBIDDEN = (
    "app.services.brokers",
    "app.services.order_proposals",
    "app.services.execution_intent_gateway.upbit_live_forwarder",
    "app.services.kis_trading_service",
    "app.mcp_server",
    "app.tasks",
    "taskiq",
)


def _loaded(module: str) -> set[str]:
    code = f"import sys\nimport {module}\nprint('\\n'.join(sorted(sys.modules)))\n"
    return set(
        subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    )


def test_monitor_import_closure_has_no_broker_or_order_module() -> None:
    # ``app.models`` registers every table and pulls constant-only modules
    # (order_proposals.state_machine/errors); anything beyond it is ours.
    extra = _loaded("app.services.daily_loss_monitor.monitor") - _loaded("app.models")
    assert sorted(m for m in extra if m.startswith(_FORBIDDEN)) == []
    assert not [
        m for m in _loaded("app.models") if m.startswith("app.services.brokers")
    ]


def test_no_scheduler_registration() -> None:
    for path in sorted((ROOT / "app/services/daily_loss_monitor").glob("*.py")):
        source = path.read_text()
        assert "taskiq" not in source.lower() and "cron" not in source.lower(), path
    assert not list((ROOT / "deploy").rglob("*daily-loss*"))
    assert not list((ROOT / "deploy").rglob("*daily_loss*"))

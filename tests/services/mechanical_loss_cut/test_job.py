"""The shadow job (ROB-1460). Fixture holdings, in-memory ports, broker trapped."""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.brokers.kis.client import KISClient
from app.services.brokers.toss.client import TossReadClient
from app.services.mechanical_loss_cut import job as job_module
from app.services.mechanical_loss_cut.job import (
    WrittenDecision,
    decision_preview,
    evaluate_all_lots,
    run_mechanical_loss_cut,
)
from app.services.mechanical_loss_cut.rules import LotKey
from app.services.mechanical_loss_cut.store import summarize_decisions
from tests.services.mechanical_loss_cut.support import (
    KEY,
    SESSION,
    FakePorts,
    bars_from_closes,
    held,
    plan,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 30, 7, 0, tzinfo=UTC)
_PACKAGE = Path(__file__).resolve().parents[3] / "app/services/mechanical_loss_cut"
_FALLING = [95000, 93000, 91000, 89000, 87000, 86000, 84000]
_RECOVERING = [70000, 71000, 72000, 74000, 76000, 78000, 80000]
_HOLDING_ABOVE_STOP = [95000, 94000, 93000, 92000, 91000, 90500, 90000]

_BREAK_KEY = KEY
_RISE_KEY = LotKey("kis_live", "equity_kr", "000660")
_ABOVE_KEY = LotKey("toss_live", "equity_kr", "005930")
_STALE_KEY = LotKey("kis_live", "equity_kr", "000880")
_PARK_KEY = LotKey("toss_live", "equity_kr", "459580")
_LEGACY_KEY = LotKey("kis_live", "equity_kr", "051910")
_COVERED_KEY = LotKey("kis_live", "equity_kr", "066570")


class _BrokerTouched(AssertionError):
    pass


@pytest.fixture
def broker_trap(monkeypatch):
    """Any attempt to build a broker client or open HTTP fails the test."""

    def refuse(*_args, **_kwargs):
        raise _BrokerTouched("the shadow path reached a broker client")

    monkeypatch.setattr(KISClient, "__init__", refuse)
    monkeypatch.setattr(TossReadClient, "__init__", refuse)
    monkeypatch.setattr("httpx.AsyncClient.send", refuse)


async def _no_prior(_keys):
    return {}


def _fixture_ports(**overrides) -> FakePorts:
    values = {
        "lots": [
            held(key=_BREAK_KEY),
            held(key=_RISE_KEY),
            held(key=_ABOVE_KEY),
            held(key=_STALE_KEY),
            held(key=_PARK_KEY),
            held(key=_LEGACY_KEY, average_cost="130000"),
            held(key=_COVERED_KEY),
        ],
        "plans": {
            _BREAK_KEY: plan(key=_BREAK_KEY),
            _RISE_KEY: plan(key=_RISE_KEY),
            _ABOVE_KEY: plan(key=_ABOVE_KEY),
            _STALE_KEY: plan(key=_STALE_KEY),
            _COVERED_KEY: plan(key=_COVERED_KEY),
        },
        "bars": {
            _BREAK_KEY.symbol: (bars_from_closes(_FALLING), True),
            _RISE_KEY.symbol: (bars_from_closes(_RECOVERING), True),
            _ABOVE_KEY.symbol: (bars_from_closes(_HOLDING_ABOVE_STOP), True),
            _STALE_KEY.symbol: (bars_from_closes(_FALLING), False),
            _PARK_KEY.symbol: (bars_from_closes(_FALLING), True),
            _LEGACY_KEY.symbol: (bars_from_closes(_FALLING), True),
            _COVERED_KEY.symbol: (bars_from_closes(_FALLING), True),
        },
        "open_sells": {_COVERED_KEY: Decimal("10")},
    }
    values.update(overrides)
    return FakePorts(**values)


@pytest.mark.asyncio
async def test_shadow_writes_rows_on_fixture_holdings_and_never_touches_a_broker(
    broker_trap, capsys
):
    ports = _fixture_ports()

    result = await evaluate_all_lots(ports, now=NOW, markets=("equity_kr",))

    assert result["lots_evaluated"] == 7
    assert result["decisions_written"] == 2
    assert {item.decision.key for item in result["decisions"]} == {
        _BREAK_KEY,
        _LEGACY_KEY,
    }
    assert result["skipped"] == {
        "above_planned_stop": 1,
        "covered_by_open_sell": 1,
        "parking_instrument": 1,
        "price_stale": 1,
        "rising": 1,
    }
    assert ports.commits == 1
    with capsys.disabled():
        print(
            "\nSHADOW_SAMPLE",
            json.dumps(
                {
                    **{k: v for k, v in result.items() if k != "decisions"},
                    "decisions": [
                        decision_preview(item) for item in result["decisions"]
                    ],
                },
                indent=2,
            ),
        )


@pytest.mark.asyncio
async def test_lot_without_a_plan_gets_a_backfilled_plan_reported_apart(broker_trap):
    ports = _fixture_ports()

    result = await evaluate_all_lots(ports, now=NOW, markets=("equity_kr",))

    assert result["plans_backfilled"] == 1
    assert [draft.key for draft in ports.backfills] == [_LEGACY_KEY]
    assert ports.backfills[0].backfilled is True
    by_key = {item.decision.key: item.decision for item in result["decisions"]}
    assert by_key[_LEGACY_KEY].backfilled is True
    assert by_key[_BREAK_KEY].backfilled is False

    rows = [
        SimpleNamespace(
            backfilled=item.decision.backfilled,
            market=item.decision.key.market,
            currency=item.decision.currency,
            intended_notional=item.decision.intended_notional,
            within_per_order_cap=False,
            symbol=item.decision.key.symbol,
        )
        for item in result["decisions"]
    ]
    summary = summarize_decisions(rows, session_date=SESSION)
    assert summary["planned"]["count"] == 1
    assert summary["backfilled"]["count"] == 1
    assert summary["backfilled"]["by_market"]["equity_kr"]["symbols"] == ["051910"]


@pytest.mark.asyncio
async def test_parking_lot_gets_no_backfilled_plan(broker_trap):
    ports = _fixture_ports()

    await evaluate_all_lots(ports, now=NOW, markets=("equity_kr",))

    assert _PARK_KEY not in ports.plans


@pytest.mark.asyncio
async def test_second_run_on_the_same_trigger_writes_nothing_new(broker_trap):
    ports = _fixture_ports()

    first = await evaluate_all_lots(ports, now=NOW, markets=("equity_kr",))
    second = await evaluate_all_lots(ports, now=NOW, markets=("equity_kr",))
    # Two runs racing cannot see each other's row. The constraint is what stops
    # the second one.
    ports.prior_decisions = _no_prior
    racing = await evaluate_all_lots(ports, now=NOW, markets=("equity_kr",))

    assert first["decisions_written"] == 2
    assert second["decisions_written"] == 0
    assert second["skipped"]["already_cut_this_run"] == 2
    assert racing["decisions_written"] == 0
    assert racing["already_recorded"] == 2
    assert len(ports.rows) == 2


@pytest.mark.asyncio
async def test_an_open_sell_read_failure_cuts_nothing(broker_trap):
    ports = _fixture_ports(open_sell_error=RuntimeError("db down"))

    result = await evaluate_all_lots(ports, now=NOW, markets=("equity_kr",))

    assert result["decisions_written"] == 0
    assert result["skipped"]["open_sell_unknown"] == 3


@pytest.mark.asyncio
async def test_dry_run_does_not_commit(broker_trap):
    ports = _fixture_ports()

    await evaluate_all_lots(ports, now=NOW, markets=("equity_kr",), commit=False)

    assert ports.commits == 0


def test_every_switch_defaults_off():
    field = type(settings).model_fields
    for name in (
        "LOT_EXIT_PLAN_WRITE_ENABLED",
        "MECHANICAL_LOSS_CUT_SHADOW_ENABLED",
        "MECHANICAL_LOSS_CUT_LIVE_ENABLED",
    ):
        assert field[name].default is False
    assert field["MECHANICAL_LOSS_CUT_LIVE_MARKETS"].default == ""
    assert field["MECHANICAL_LOSS_CUT_LIVE_ACCOUNT_MODES"].default == ""


@pytest.mark.asyncio
async def test_default_does_nothing_at_all(monkeypatch):
    monkeypatch.setattr(settings, "MECHANICAL_LOSS_CUT_SHADOW_ENABLED", False)

    def no_session():
        raise AssertionError("a disabled job opened a session")

    result = await run_mechanical_loss_cut(now=NOW, session_factory=no_session)

    assert result == {"status": "disabled"}


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def rollback(self):
        return None


@pytest.mark.asyncio
async def test_shadow_on_live_off_never_reaches_the_live_emitter(
    monkeypatch, broker_trap
):
    monkeypatch.setattr(settings, "MECHANICAL_LOSS_CUT_SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "MECHANICAL_LOSS_CUT_LIVE_ENABLED", False)
    ports = _fixture_ports()
    monkeypatch.setattr(job_module, "DatabasePorts", lambda _session: ports)

    async def emitter(_written, _now):
        raise AssertionError("shadow reached the live emitter")

    result = await run_mechanical_loss_cut(
        now=NOW,
        markets=("equity_kr",),
        session_factory=_FakeSession,
        live_emitter=emitter,
    )

    assert result["status"] == "ok"
    assert result["decisions_written"] == 2
    assert "live" not in result


@pytest.mark.asyncio
async def test_live_on_hands_written_decisions_to_the_emitter(monkeypatch):
    monkeypatch.setattr(settings, "MECHANICAL_LOSS_CUT_SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "MECHANICAL_LOSS_CUT_LIVE_ENABLED", True)
    ports = _fixture_ports()
    monkeypatch.setattr(job_module, "DatabasePorts", lambda _session: ports)
    seen: list[WrittenDecision] = []

    async def emitter(written, _now):
        seen.extend(written)
        return {"outcomes": {}}

    result = await run_mechanical_loss_cut(
        now=NOW,
        markets=("equity_kr",),
        session_factory=_FakeSession,
        live_emitter=emitter,
    )

    assert len(seen) == 2
    assert result["live"] == {"outcomes": {}}


@pytest.mark.asyncio
async def test_dry_run_never_reaches_the_live_emitter_even_with_the_switch_on(
    monkeypatch,
):
    monkeypatch.setattr(settings, "MECHANICAL_LOSS_CUT_SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "MECHANICAL_LOSS_CUT_LIVE_ENABLED", True)
    ports = _fixture_ports()
    monkeypatch.setattr(job_module, "DatabasePorts", lambda _session: ports)

    async def emitter(_written, _now):
        raise AssertionError("a dry run reached the live emitter")

    result = await run_mechanical_loss_cut(
        now=NOW,
        markets=("equity_kr",),
        commit=False,
        session_factory=_FakeSession,
        live_emitter=emitter,
    )

    assert result["status"] == "dry_run"


_FORBIDDEN_IMPORTS = (
    "app.services.brokers",
    "app.mcp_server.tooling.order_execution",
    "app.mcp_server.tooling.order_validation",
    "app.mcp_server.tooling.orders_",
    "app.services.order_proposals.dispatch",
    "app.services.order_proposals.revalidation",
    "app.services.order_proposals.broker_gateway",
)


@pytest.mark.parametrize(
    "module", ["rules.py", "gate.py", "store.py", "positions.py", "plans.py", "job.py"]
)
def test_shadow_modules_import_no_broker_or_order_path(module):
    """``live.py`` is the one module allowed to reach the order path."""
    tree = ast.parse((_PACKAGE / module).read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    allowed = {"app.services.mechanical_loss_cut.live"} if module == "job.py" else set()
    offenders = [
        name
        for name in imported
        if name.startswith(_FORBIDDEN_IMPORTS) and name not in allowed
    ]
    assert offenders == []


def test_job_loads_the_live_module_only_inside_the_switch_branch():
    tree = ast.parse((_PACKAGE / "job.py").read_text())
    top_level = [
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
    ]
    assert "app.services.mechanical_loss_cut.live" not in top_level


def test_task_ships_with_no_schedule():
    from app.tasks.mechanical_loss_cut_tasks import mechanical_loss_cut_evaluate_task

    assert "schedule" not in mechanical_loss_cut_evaluate_task.labels

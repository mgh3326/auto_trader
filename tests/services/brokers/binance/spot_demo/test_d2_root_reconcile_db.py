"""#1268 — DB-backed tests for the D2 root reconcile (test DB only, fake broker).

The three D2 roots are created the way the writer creates them (committed
planned claim, then the service's own transitions up to ``filled``) and deleted
afterwards. The broker is a read-only fake; no socket is opened.
"""

from __future__ import annotations

import argparse
import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.core.db import AsyncSessionLocal, engine
from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService
from app.services.brokers.binance.h5 import truth_gate
from app.services.brokers.binance.spot_demo import d2_root_reconcile as r
from scripts import binance_spot_demo_d2_root_reconcile as cli
from tests._run_owned_database import validate_run_owned_database_url
from tests.services.brokers.binance.spot_demo._d2_root_fixtures import (
    BTC,
    ETH,
    USDC,
    FakeSpotDemoReader,
    d2_metadata,
    filled_body,
)

validate_run_owned_database_url(engine.url)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

REASON = "hk 1268: D2 remediation SELLs filled 2026-08-21; broker shows FILLED"
ACTOR = "operator-desk"
_OTHER_PREFIX = "d2rtest-"
_FILLED_AT = dt.datetime(2026, 8, 21, 9, 15, tzinfo=dt.UTC)


async def _purge() -> None:
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(
            text(
                "DELETE FROM binance_demo_order_ledger "
                "WHERE client_order_id LIKE 'd2rem-%' "
                "OR client_order_id LIKE :other"
            ),
            {"other": f"{_OTHER_PREFIX}%"},
        )
        await cleanup.commit()


@pytest_asyncio.fixture(autouse=True)
async def _clean():
    await _purge()
    yield
    await _purge()


async def _instrument(ledger: BinanceDemoLedgerService, symbol: str, product="spot"):
    return await ledger.resolve_or_create_instrument(
        venue="binance",
        product=product,
        venue_symbol=symbol,
        base_asset=symbol.removesuffix("USDT"),
        quote_asset="USDT",
    )


async def _filled_root(
    *,
    client_order_id: str,
    symbol: str,
    qty: Decimal,
    price: Decimal,
    broker_order_id: str,
    metadata: dict[str, Any],
    product: str = "spot",
    venue_host: str = "demo-api.binance.com",
    final_state: str = "filled",
) -> int:
    async with AsyncSessionLocal() as session:
        ledger = BinanceDemoLedgerService(session)
        instrument_id = await _instrument(ledger, symbol, product)
        await ledger.commit_planned_claim(
            instrument_id=instrument_id,
            product=product,
            venue_host=venue_host,
            client_order_id=client_order_id,
            side="SELL",
            order_type="LIMIT",
            qty=qty,
            price=price,
            extra_metadata=metadata,
            now=_FILLED_AT,
        )
        await ledger.record_previewed(client_order_id=client_order_id, now=_FILLED_AT)
        await ledger.record_validated(client_order_id=client_order_id, now=_FILLED_AT)
        await ledger.record_submitted(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            now=_FILLED_AT,
        )
        row = await ledger.record_filled(
            client_order_id=client_order_id, now=_FILLED_AT
        )
        if final_state == "anomaly":
            row = await ledger.record_anomaly(
                client_order_id=client_order_id, reason="test", now=_FILLED_AT
            )
        row_id = int(row.id)
        await session.commit()
        return row_id


async def _d2_roots() -> dict[str, int]:
    ids: dict[str, int] = {}
    for index, order in enumerate((BTC, ETH, USDC)):
        ids[order.symbol] = await _filled_root(
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            qty=order.quantity,
            price=order.price,
            broker_order_id=str(7000 + index),
            metadata=d2_metadata(order, f"op-{order.symbol}"),
        )
    return ids


def _reader(**overrides: Any) -> FakeSpotDemoReader:
    answers = {
        order.client_order_id: filled_body(order, 7000 + index)
        for index, order in enumerate((BTC, ETH, USDC))
    }
    answers.update(overrides)
    return FakeSpotDemoReader(answers)


async def _snapshot() -> list[tuple]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(
                BinanceDemoOrderLedger.id,
                BinanceDemoOrderLedger.lifecycle_state,
                BinanceDemoOrderLedger.updated_at,
                BinanceDemoOrderLedger.closed_at,
                BinanceDemoOrderLedger.reconciled_at,
                BinanceDemoOrderLedger.extra_metadata,
            ).order_by(BinanceDemoOrderLedger.id)
        )
        return [tuple(row) for row in result.all()]


async def _states(ids) -> dict[int, str]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(
                BinanceDemoOrderLedger.id, BinanceDemoOrderLedger.lifecycle_state
            ).where(BinanceDemoOrderLedger.id.in_(list(ids)))
        )
        return dict(result.all())


async def _gate_ledger_check() -> truth_gate.GateCheck:
    async with AsyncSessionLocal() as session:
        return await truth_gate._ledger(BinanceDemoLedgerService(session))


async def _preview(client, ids):
    async with AsyncSessionLocal() as session:
        return await r.preview_reconcile(session, client, ids)


async def _commit(client, ids, **kwargs):
    async with AsyncSessionLocal() as session:
        return await r.commit_reconcile(
            session, client, ids, reason=REASON, actor=ACTOR, **kwargs
        )


# ------------------------------------------------------------------ A1


async def test_a1_preview_changes_nothing_and_reports_eligible() -> None:
    ids = tuple((await _d2_roots()).values())
    before = await _snapshot()
    client = _reader()
    result = await _preview(client, ids)
    assert result.status == "eligible"
    assert all(row.eligible for row in result.rows)
    assert await _snapshot() == before
    assert sorted(c for _, c in client.calls) == sorted(
        o.client_order_id for o in (BTC, ETH, USDC)
    )


async def test_a1_commit_moves_exactly_the_roots_then_second_commit_is_noop() -> None:
    roots = await _d2_roots()
    ids = tuple(roots.values())
    bystander = await _filled_root(
        client_order_id=f"{_OTHER_PREFIX}{uuid.uuid4().hex[:12]}",
        symbol="SOLUSDT",
        qty=Decimal("1"),
        price=Decimal("150"),
        broker_order_id="8000",
        metadata={"writer": "demo_scalping"},
    )
    moment = dt.datetime(2026, 10, 8, 3, 0, tzinfo=dt.UTC)

    result = await _commit(_reader(), ids, now=moment)
    assert result.status == "committed"
    assert result.changed == 3
    assert await _states(ids) == dict.fromkeys(ids, "reconciled")
    assert (await _states([bystander]))[bystander] == "filled"

    async with AsyncSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(BinanceDemoOrderLedger).where(
                        BinanceDemoOrderLedger.id.in_(ids)
                    )
                )
            )
            .scalars()
            .all()
        )
    for row in rows:
        # Legal path: filled -> closed -> reconciled, each stamped by the service.
        assert row.filled_at == _FILLED_AT
        assert row.closed_at == moment
        assert row.reconciled_at == moment
        assert row.last_reconciled_at == moment
        audit = row.extra_metadata[r.AUDIT_KEY]
        assert audit["tool"] == r.TOOL_NAME
        assert audit["reason"] == REASON
        assert audit["actor"] == ACTOR
        assert audit["ledger_id"] == row.id
        assert audit["batch_id"] == str(result.batch_id)
        assert audit["broker_evidence"]["status"] == "FILLED"
        assert audit["broker_evidence"]["clientOrderId"] == row.client_order_id
        assert str(audit["broker_evidence"]["orderId"]) == row.broker_order_id
        # The D2 writer's own evidence and immutable fill actuals are untouched.
        assert row.extra_metadata["writer"] == "d2_remediation_single"
        assert "filled_qty" not in row.extra_metadata

    after_first = await _snapshot()
    client = _reader()
    again = await _commit(client, ids)
    assert again.status == "noop"
    assert again.changed == 0
    assert client.calls == []
    assert await _snapshot() == after_first


async def test_a1_subset_then_rest_and_mixed_batch_refused() -> None:
    roots = await _d2_roots()
    first = (roots["BTCUSDT"], roots["ETHUSDT"])
    assert (await _commit(_reader(), first)).status == "committed"
    before = await _snapshot()
    mixed = await _commit(_reader(), tuple(roots.values()))
    assert mixed.status == "refused"
    assert await _snapshot() == before
    rest = await _commit(_reader(), (roots["USDCUSDT"],))
    assert rest.status == "committed"
    assert await _states(roots.values()) == dict.fromkeys(roots.values(), "reconciled")


# ------------------------------------------------------------------ A2


async def test_a2_one_ineligible_id_refuses_the_batch() -> None:
    ids = tuple((await _d2_roots()).values())
    futures = await _filled_root(
        client_order_id=f"{_OTHER_PREFIX}{uuid.uuid4().hex[:12]}",
        symbol="XRPUSDT",
        qty=Decimal("10"),
        price=Decimal("0.5"),
        broker_order_id="8001",
        metadata=d2_metadata(BTC),
        product="usdm_futures",
        venue_host="demo-fapi.binance.com",
    )
    before = await _snapshot()
    client = _reader()
    result = await _commit(client, (*ids[:2], futures))
    assert result.status == "refused"
    assert futures in result.refused_ids
    assert await _snapshot() == before


@pytest.mark.parametrize(
    "case",
    ["not_d2_writer", "anomaly", "missing"],
)
async def test_a2_non_remediation_terminal_or_missing_id_refuses(case: str) -> None:
    roots = await _d2_roots()
    if case == "not_d2_writer":
        bad = await _filled_root(
            client_order_id=f"{_OTHER_PREFIX}{uuid.uuid4().hex[:12]}",
            symbol="DOGEUSDT",
            qty=Decimal("10"),
            price=Decimal("0.1"),
            broker_order_id="8002",
            metadata={"writer": "binance_demo_strategy_loop"},
        )
    elif case == "anomaly":
        bad = await _filled_root(
            client_order_id=f"{_OTHER_PREFIX}{uuid.uuid4().hex[:12]}",
            symbol="DOGEUSDT",
            qty=Decimal("10"),
            price=Decimal("0.1"),
            broker_order_id="8003",
            metadata=d2_metadata(BTC),
            final_state="anomaly",
        )
    else:
        bad = max(roots.values()) + 100_000
    ids = (roots["BTCUSDT"], roots["ETHUSDT"], bad)
    before = await _snapshot()
    result = await _commit(_reader(), ids)
    assert result.status == "refused"
    assert result.refused_ids == [bad]
    assert await _snapshot() == before


@pytest.mark.parametrize(
    "answer",
    [
        {"status": "PARTIALLY_FILLED"},
        {"price": "2368.47000000"},
        {"executedQty": "0.00510000"},
        {"orderId": 9999},
        "not_found",
        "read_error",
    ],
)
async def test_a2_evidence_mismatch_or_missing_refuses_the_batch(answer) -> None:
    ids = tuple((await _d2_roots()).values())
    if answer == "not_found":
        override: Any = None
    elif answer == "read_error":
        override = ConnectionError("reset")
    else:
        override = {**filled_body(ETH, 7001), **answer}
    client = _reader()
    client.answers[ETH.client_order_id] = override
    if override is None:
        del client.answers[ETH.client_order_id]
    before = await _snapshot()
    result = await _commit(client, ids)
    assert result.status == "refused"
    refused = [row for row in result.rows if not row.eligible]
    assert [row.ledger_id for row in refused] == [ids[1]]
    assert await _snapshot() == before


async def test_a2_failure_mid_transitions_leaves_nothing_applied(monkeypatch) -> None:
    ids = tuple((await _d2_roots()).values())
    before = await _snapshot()
    real = BinanceDemoLedgerService.record_reconciled
    calls = {"n": 0}

    async def flaky(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("crash between transitions")
        return await real(self, **kwargs)

    monkeypatch.setattr(BinanceDemoLedgerService, "record_reconciled", flaky)
    with pytest.raises(RuntimeError):
        await _commit(_reader(), ids)
    assert await _snapshot() == before


async def test_a2_live_host_client_reads_and_writes_nothing() -> None:
    ids = tuple((await _d2_roots()).values())
    before = await _snapshot()
    client = FakeSpotDemoReader(_reader().answers, base_url="https://api.binance.com")
    with pytest.raises(r.D2RootReconcileInputError):
        await _commit(client, ids)
    assert client.calls == []
    assert await _snapshot() == before


# ------------------------------------------------------------------ A3


async def test_a3_truth_gate_ledger_check_passes_after_commit() -> None:
    ids = tuple((await _d2_roots()).values())
    before = await _gate_ledger_check()
    assert before.name == "demo_ledger_no_open_roots"
    assert not before.ok
    assert "open_roots=3" in before.detail
    assert "filled=3" in before.detail

    assert (await _commit(_reader(), ids)).status == "committed"

    after = await _gate_ledger_check()
    assert after.ok, after.detail
    assert after.detail.startswith("open_roots=0")
    assert "reconciled=3" in after.detail


# ------------------------------------------------------------------ CLI


def _args(ids: str, *, commit: bool) -> argparse.Namespace:
    return argparse.Namespace(
        database_url=None,
        database_url_env="UNUSED",
        ids=[ids],
        reason=REASON,
        actor=ACTOR,
        commit=commit,
    )


async def test_cli_preview_then_commit_then_noop_exit_codes() -> None:
    ids = tuple((await _d2_roots()).values())
    joined = ",".join(str(i) for i in ids)
    clients: list[FakeSpotDemoReader] = []

    def factory() -> FakeSpotDemoReader:
        clients.append(_reader())
        return clients[-1]

    code, payload = await cli.run(
        _args(joined, commit=False),
        session_factory=AsyncSessionLocal,
        client_factory=factory,
    )
    assert (code, payload["mode"], payload["status"]) == (0, "preview", "eligible")
    assert payload["broker_mutation_count"] == 0
    assert await _states(ids) == dict.fromkeys(ids, "filled")

    code, payload = await cli.run(
        _args(joined, commit=True),
        session_factory=AsyncSessionLocal,
        client_factory=factory,
    )
    assert (code, payload["status"], payload["changed"]) == (0, "committed", 3)

    code, payload = await cli.run(
        _args(joined, commit=True),
        session_factory=AsyncSessionLocal,
        client_factory=factory,
    )
    assert (code, payload["status"]) == (0, "noop")
    assert all(client.closed for client in clients)


async def test_cli_refused_batch_exits_2() -> None:
    ids = tuple((await _d2_roots()).values())
    bad = _reader()
    bad.answers[BTC.client_order_id] = {**filled_body(BTC, 7000), "status": "NEW"}
    code, payload = await cli.run(
        _args(",".join(str(i) for i in ids), commit=True),
        session_factory=AsyncSessionLocal,
        client_factory=lambda: bad,
    )
    assert code == 2
    assert payload["status"] == "refused"
    assert await _states(ids) == dict.fromkeys(ids, "filled")

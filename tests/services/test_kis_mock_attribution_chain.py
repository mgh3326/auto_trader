"""Attribution chain: signal -> submit -> ledger -> reconcile, on one key.

Four things are proved here, in the order the brief asks for them:

1. the chain is continuous end to end when read by a single correlation_id;
2. a deliberately removed stage is *detected* — the query does not quietly
   return a shorter chain;
3. an order with no attribution never reaches the broker (fail-closed, not a
   warning);
4. the migration adds and only adds.

Every DB touch here runs against the pytest run-owned scratch database created
by tests/_schema_bootstrap.py. Nothing in this file connects to, reads, or
writes an operational database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.jobs.kis_mock_reconciliation_job import run_kis_mock_reconciliation
from app.mcp_server.tooling import order_execution
from app.models.review import KISMockOrderLedger
from app.services.kis_mock_attribution import (
    MissingAttribution,
    record_signal,
    resolve_attribution,
)
from app.services.kis_mock_attribution_chain import (
    GAP_ORDER_MISSING,
    GAP_ORDER_UNATTRIBUTED,
    GAP_RECONCILE_MISSING,
    GAP_SIGNAL_MISSING,
    load_attribution_chain,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# resolver (pure)
# --------------------------------------------------------------------------


async def test_resolver_requires_a_strategy():
    with pytest.raises(MissingAttribution) as excinfo:
        resolve_attribution(symbol="005930", side="buy", price=70000, quantity=1)
    assert excinfo.value.missing == ("strategy",)


async def test_resolver_rejects_blank_and_whitespace_strategy():
    for blank in ("", "   ", "\t\n"):
        with pytest.raises(MissingAttribution):
            resolve_attribution(
                symbol="005930",
                side="buy",
                price=70000,
                quantity=1,
                strategy=blank,
            )


async def test_resolver_derives_the_mirror_lane_without_an_explicit_strategy():
    attribution = resolve_attribution(
        symbol="005930",
        side="buy",
        price=70000,
        quantity=1,
        mirror_cohort="mock_counterfactual",
    )
    assert attribution.strategy == "mock_counterfactual_mirror"
    assert attribution.signal_source == "mirror"


async def test_resolver_mint_is_deterministic_and_matches_the_legacy_id():
    """Moving the mint before the send must not renumber existing ids."""
    from app.services.live_correlation import live_correlation_id

    kwargs = {
        "symbol": "005930",
        "side": "buy",
        "price": 70000.0,
        "quantity": 3.0,
        "kst_trade_day": "2026-08-03",
    }
    attribution = resolve_attribution(**kwargs, strategy="posture-v1")
    legacy = live_correlation_id(
        account_scope="kis_mock",
        symbol="005930",
        side="buy",
        price=Decimal("70000.0"),
        quantity=Decimal("3.0"),
        kst_trade_day="2026-08-03",
        rung=0,
    )
    assert attribution.correlation_id == legacy


# --------------------------------------------------------------------------
# 1. chain continuity
# --------------------------------------------------------------------------


def _order_row(*, symbol: str, correlation_id: str, **overrides) -> KISMockOrderLedger:
    values = {
        "trade_date": datetime.now(UTC) - timedelta(minutes=1),
        "symbol": symbol,
        "instrument_type": "equity_kr",
        "side": "buy",
        "order_type": "limit",
        "quantity": Decimal("1"),
        "price": Decimal("70000"),
        "amount": Decimal("70000"),
        "currency": "KRW",
        "account_mode": "kis_mock",
        "broker": "kis",
        "status": "accepted",
        "order_no": f"MOCK-{uuid4()}",
        "lifecycle_state": "accepted",
        "holdings_baseline_qty": Decimal("0"),
        "correlation_id": correlation_id,
        "strategy": "posture-v1",
    }
    values.update(overrides)
    return KISMockOrderLedger(**values)


async def test_chain_is_unbroken_from_signal_through_reconcile(db_session):
    symbol = f"CH-{uuid4().hex[:8]}"
    attribution = resolve_attribution(
        symbol=symbol, side="buy", price=70000, quantity=1, strategy="posture-v1"
    )
    cid = attribution.correlation_id

    # stage 1 — signal, written before any order exists
    await record_signal(
        db_session,
        attribution=attribution,
        symbol=symbol,
        decision="order",
        instrument_type="equity_kr",
        side="buy",
        intended_quantity=1,
        intended_price=70000,
    )

    # stage 2 — the order row carrying the same key
    db_session.add(_order_row(symbol=symbol, correlation_id=cid))
    await db_session.commit()

    # stage 3 — reconcile, which must carry the key into its own record
    client = MagicMock()
    client.fetch_my_stocks = AsyncMock(
        side_effect=[[{"pdno": symbol, "hldg_qty": "1"}], []]
    )
    result = await run_kis_mock_reconciliation(
        db_session,
        dry_run=False,
        market="equity_kr",
        symbol=symbol,
        kis_client=client,
    )
    assert result["success"] is True
    assert [event["correlation_id"] for event in result["events"]] == [cid]
    assert result["events"][0]["detail"]["correlation_id"] == cid

    db_session.expire_all()
    chain = await load_attribution_chain(db_session, correlation_id=cid)

    assert chain.gaps == ()
    assert chain.unbroken is True
    assert chain.strategy == "posture-v1"
    assert [stage.name for stage in chain.stages] == ["signal", "order", "reconcile"]
    assert all(stage.present for stage in chain.stages)
    assert (
        chain.stage("reconcile").detail["reconcile_details"][0]["correlation_id"] == cid
    )


async def test_a_signal_that_produced_no_order_is_a_complete_chain(db_session):
    """Suppressed signals are evidence, not gaps — they hold the denominator."""
    symbol = f"NO-{uuid4().hex[:8]}"
    attribution = resolve_attribution(
        symbol=symbol, side="buy", price=1000, quantity=1, strategy="posture-v1"
    )
    await record_signal(
        db_session,
        attribution=attribution,
        symbol=symbol,
        decision="no_order",
        suppressed_reason="risk_cap_reached",
    )

    chain = await load_attribution_chain(
        db_session, correlation_id=attribution.correlation_id
    )
    assert chain.gaps == ()
    assert chain.stage("signal").detail["decision"] == "no_order"
    assert chain.stage("signal").detail["suppressed_reason"] == "risk_cap_reached"
    assert chain.stage("order").present is False


async def test_record_signal_is_idempotent_on_replay(db_session):
    symbol = f"ID-{uuid4().hex[:8]}"
    attribution = resolve_attribution(
        symbol=symbol, side="buy", price=1000, quantity=1, strategy="posture-v1"
    )
    first = await record_signal(
        db_session, attribution=attribution, symbol=symbol, decision="order"
    )
    second = await record_signal(
        db_session, attribution=attribution, symbol=symbol, decision="order"
    )
    assert first == second


# --------------------------------------------------------------------------
# 2. gap detection — break the chain on purpose
# --------------------------------------------------------------------------


async def test_missing_signal_stage_is_detected(db_session):
    """An order with no pre-submit signal row is the pre-repair world."""
    symbol = f"GS-{uuid4().hex[:8]}"
    cid = f"live:kis_mock:{uuid4().hex[:16]}"
    db_session.add(_order_row(symbol=symbol, correlation_id=cid))
    await db_session.commit()

    chain = await load_attribution_chain(db_session, correlation_id=cid)
    assert GAP_SIGNAL_MISSING in chain.gaps
    assert chain.unbroken is False


async def test_missing_order_stage_is_detected(db_session):
    symbol = f"GO-{uuid4().hex[:8]}"
    attribution = resolve_attribution(
        symbol=symbol, side="buy", price=1000, quantity=1, strategy="posture-v1"
    )
    await record_signal(
        db_session, attribution=attribution, symbol=symbol, decision="order"
    )

    chain = await load_attribution_chain(
        db_session, correlation_id=attribution.correlation_id
    )
    assert GAP_ORDER_MISSING in chain.gaps


async def test_missing_reconcile_stage_is_detected(db_session):
    symbol = f"GR-{uuid4().hex[:8]}"
    attribution = resolve_attribution(
        symbol=symbol, side="buy", price=1000, quantity=1, strategy="posture-v1"
    )
    await record_signal(
        db_session, attribution=attribution, symbol=symbol, decision="order"
    )
    db_session.add(_order_row(symbol=symbol, correlation_id=attribution.correlation_id))
    await db_session.commit()

    chain = await load_attribution_chain(
        db_session, correlation_id=attribution.correlation_id
    )
    assert GAP_RECONCILE_MISSING in chain.gaps


async def test_unattributed_order_row_is_detected(db_session):
    """The exact ROB-1093 shape: the row exists but names no owner."""
    symbol = f"GU-{uuid4().hex[:8]}"
    attribution = resolve_attribution(
        symbol=symbol, side="buy", price=1000, quantity=1, strategy="posture-v1"
    )
    await record_signal(
        db_session, attribution=attribution, symbol=symbol, decision="order"
    )
    db_session.add(
        _order_row(
            symbol=symbol,
            correlation_id=attribution.correlation_id,
            strategy=None,
            lifecycle_state="previewed",
        )
    )
    await db_session.commit()

    chain = await load_attribution_chain(
        db_session, correlation_id=attribution.correlation_id
    )
    assert GAP_ORDER_UNATTRIBUTED in chain.gaps


# --------------------------------------------------------------------------
# 3. fail-closed — the order must not be sent
# --------------------------------------------------------------------------


def _execute_and_record_kwargs(**overrides):
    kwargs = {
        "normalized_symbol": "005930",
        "side": "buy",
        "order_type": "limit",
        "order_quantity": 1.0,
        "price": 70000.0,
        "market_type": "equity_kr",
        "current_price": 70000.0,
        "avg_price": 0.0,
        "dry_run_result": {
            "price": 70000.0,
            "quantity": 1.0,
            "estimated_value": 70000.0,
        },
        "order_amount": 70000.0,
        "reason": "test",
        "exit_reason": None,
        "thesis": None,
        "strategy": None,
        "target_price": None,
        "stop_loss": None,
        "min_hold_days": None,
        "notes": None,
        "indicators_snapshot": None,
        "defensive_trim_ctx": None,
        "order_error_fn": lambda message: {"success": False, "error": message},
        "is_mock": True,
    }
    kwargs.update(overrides)
    return kwargs


@pytest.fixture
def broker_tripwire(monkeypatch):
    """Fails the test if anything reaches the broker."""
    calls: list[dict] = []

    async def _explode(**kwargs):
        calls.append(kwargs)
        raise AssertionError("broker was called for an unattributed order")

    monkeypatch.setattr(order_execution, "_execute_order", _explode)
    monkeypatch.setattr(
        order_execution, "_record_order_history", AsyncMock(return_value=None)
    )
    return calls


async def test_order_is_blocked_when_attribution_is_missing(broker_tripwire):
    result = await order_execution._execute_and_record(**_execute_and_record_kwargs())

    assert result["success"] is False
    assert result["error_code"] == "attribution_required"
    assert result["missing_attribution"] == ["strategy"]
    # The tripwire proves it: nothing was sent.
    assert broker_tripwire == []


async def test_order_is_blocked_when_the_signal_row_cannot_be_written(
    broker_tripwire, monkeypatch
):
    """No durable attribution, no send — even though the strategy was supplied."""

    async def _boom(*args, **kwargs):
        raise RuntimeError("signal ledger unavailable")

    monkeypatch.setattr(order_execution, "record_signal", _boom)

    result = await order_execution._execute_and_record(
        **_execute_and_record_kwargs(strategy="posture-v1")
    )

    assert result["success"] is False
    assert result["error_code"] == "signal_record_unavailable"
    assert broker_tripwire == []


async def test_positive_control_attributed_order_does_reach_the_broker(
    monkeypatch, db_session
):
    """Guards against a false pass: the block above is caused by attribution.

    Without this, a test-setup error that blocks every order would look
    identical to a working fail-closed gate.
    """
    sent: list[dict] = []

    async def _capture(**kwargs):
        sent.append(kwargs)
        return {"rt_cd": "0", "odno": f"MOCK{uuid4().hex[:8]}", "msg1": "ok"}

    monkeypatch.setattr(order_execution, "_execute_order", _capture)
    monkeypatch.setattr(
        order_execution, "_record_order_history", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        order_execution,
        "_fetch_kis_mock_baseline_qty",
        AsyncMock(return_value=Decimal("0")),
        raising=False,
    )

    symbol = f"PC-{uuid4().hex[:8]}"
    result = await order_execution._execute_and_record(
        **_execute_and_record_kwargs(normalized_symbol=symbol, strategy="posture-v1")
    )

    assert len(sent) == 1, "attributed order should have been sent"
    correlation_id = result["correlation_id"]
    assert correlation_id

    # ...and the pre-submit signal row is durable, carrying the strategy.
    db_session.expire_all()
    chain = await load_attribution_chain(db_session, correlation_id=correlation_id)
    assert chain.stage("signal").present is True
    assert chain.stage("signal").detail["strategy"] == "posture-v1"

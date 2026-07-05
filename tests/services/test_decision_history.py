"""ROB-711 — decision_history service unit tests.

Covers the symbol-keyed aggregation of past judgments, lessons, outcomes,
fills, open claims, and Brier calibration used to inject per-symbol context
into ``analyze_stock_batch`` responses.

xdist isolation: ``db_session`` yields a real committing session on a SHARED
database with no per-test rollback, and CI runs many suites in parallel. Popular
real symbols (000660/005930/KRW-JUP) collide with other workers' committed rows,
so every exact-count assertion here uses a PER-TEST UNIQUE synthetic symbol.
Seeds are written with ``flush()`` (visible to the same session, never committed
→ no cross-worker pollution). Rows are seeded under the NORMALIZED symbol and
queried with the raw form — the service applies the same normalization, so they
match regardless of what normalization does.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReport, InvestmentReportItem
from app.models.review import (
    KISLiveOrderLedger,
    TradeForecast,
    TradeRetrospective,
)
from app.services.decision_history import build_decision_context
from app.services.trade_journal.forecast_service import _normalize_symbol_for_filter

# ROB-723: these tests use the global committing ``db_session`` and touch the
# investment_reports family, so serialize their review-table access under the
# shared cleanup lock (same lock the helper ``session`` fixture holds) to avoid
# racing another worker's TRUNCATE.
pytestmark = pytest.mark.usefixtures("investment_reports_cleanup_lock")


def _uniq_symbol() -> str:
    """A per-test symbol unique across the whole run (xdist-safe)."""
    return "DH" + uuid.uuid4().hex[:10].upper()


async def _make_report(db: AsyncSession, **overrides) -> InvestmentReport:
    payload = {
        "report_uuid": uuid.uuid4(),
        "idempotency_key": f"key-{uuid.uuid4()}",
        "report_type": "kr_morning",
        "market": "kr",
        "market_session": "regular",
        "account_scope": "kis_mock",
        "execution_mode": "mock_preview",
        "created_by_profile": "test",
        "title": "t",
        "summary": "s",
        "status": "draft",
    }
    payload.update(overrides)
    row = InvestmentReport(**payload)
    db.add(row)
    await db.flush()
    return row


async def _add_item(
    db: AsyncSession, report_id: int, *, symbol: str, **overrides
) -> None:
    payload = {
        "report_id": report_id,
        "item_uuid": uuid.uuid4(),
        "idempotency_key": f"item-{uuid.uuid4()}",
        "item_kind": "action",
        "symbol": symbol,
        "intent": "buy_review",
        "rationale": "지지선 눌림 재진입",
        "evidence_snapshot": {},
        "created_at": datetime(2026, 6, 1, tzinfo=UTC),
    }
    payload.update(overrides)
    db.add(InvestmentReportItem(**payload))
    await db.flush()


async def _add_retro(db: AsyncSession, *, symbol: str, **overrides) -> None:
    payload = {
        "symbol": symbol,
        "instrument_type": "equity_kr",
        "account_mode": "upbit_live",
        "outcome": "filled",
        "side": "sell",
        "strategy_key": "resistance_ladder",
        "correlation_id": f"live:{uuid.uuid4()}",
        "realized_pnl": Decimal("33914.0000"),
        "realized_pnl_currency": "KRW",
        "realized_pnl_source": "caller_supplied",
        "pnl_pct": Decimal("11.9000"),
        "trigger_type": "fill",
        "lesson": "앵커+러너 분할이 작동",
        "next_strategy": None,
    }
    payload.update(overrides)
    db.add(TradeRetrospective(**payload))
    await db.flush()


@pytest.mark.asyncio
async def test_prior_decisions_newest_first_capped_and_smoke_filtered(
    db_session: AsyncSession,
) -> None:
    raw = _uniq_symbol()
    sym = _normalize_symbol_for_filter(raw, "equity_kr")
    report = await _make_report(db_session)
    # 8 real + 1 smoke; expect newest-6 real, smoke excluded
    for i in range(8):
        await _add_item(
            db_session,
            report.id,
            symbol=sym,
            confidence=60 + i,
            rationale=f"real decision {i}",
            created_at=datetime(2026, 6, 1 + i, tzinfo=UTC),
        )
    await _add_item(
        db_session,
        report.id,
        symbol=sym,
        rationale="Smoke-only action review item",
        created_at=datetime(2026, 6, 20, tzinfo=UTC),
    )
    await db_session.flush()

    ctx = await build_decision_context(db_session, symbol=raw, market="kr")

    assert ctx is not None
    assert ctx["symbol"] == sym
    assert ctx["market"] == "kr"
    assert ctx["link_quality"] == "symbol_window"
    decisions = ctx["prior_decisions"]
    assert len(decisions) == 6  # capped
    assert decisions[0]["rationale"] == "real decision 7"  # newest first
    assert all("Smoke" not in d["rationale"] for d in decisions)  # smoke excluded


@pytest.mark.asyncio
async def test_lessons_and_outcomes_smoke_filtered_and_capped(
    db_session: AsyncSession,
) -> None:
    raw = _uniq_symbol()
    sym = _normalize_symbol_for_filter(raw, "equity_kr")
    await _add_retro(db_session, symbol=sym, lesson="real lesson A")
    await _add_retro(db_session, symbol=sym, lesson="real lesson B")
    # smoke row: created_by_profile / strategy_key / correlation_id carry SMOKE marker
    await _add_retro(
        db_session,
        symbol=sym,
        created_by_profile="HERMES_OPERATOR_SMOKE",
        strategy_key="rob474_smoke_x",
        correlation_id="rob474-smoke-x",
        lesson="correlation_id upsert is idempotent",
    )
    await db_session.flush()

    ctx = await build_decision_context(db_session, symbol=raw, market="kr")

    assert ctx is not None
    assert "real lesson A" in ctx["prior_lessons"]
    assert "real lesson B" in ctx["prior_lessons"]
    assert all("idempotent" not in les for les in ctx["prior_lessons"])
    assert len(ctx["realized_outcomes"]) == 2  # smoke excluded
    first = ctx["realized_outcomes"][0]
    assert first["pnl_pct"] == 11.9
    assert first["realized_pnl"] == 33914.0
    assert first["outcome"] == "filled"


@pytest.mark.asyncio
async def test_recent_fills_and_open_claims(db_session: AsyncSession) -> None:
    raw = _uniq_symbol()
    sym = _normalize_symbol_for_filter(raw, "equity_kr")
    db_session.add(
        KISLiveOrderLedger(
            trade_date=datetime(2026, 6, 10, tzinfo=UTC),
            symbol=sym,
            instrument_type="equity_kr",
            side="buy",
            order_type="limit",
            account_mode="kis_live",
            broker="kis",
            status="filled",
            lifecycle_state="filled",
            order_no="A1",
            quantity=Decimal("1"),
            filled_qty=Decimal("1"),
            avg_fill_price=Decimal("2000000"),
            target_price=Decimal("3035000"),
            stop_loss=Decimal("1888000"),
        )
    )
    db_session.add(
        TradeForecast(
            created_by="claude",
            symbol=sym,
            instrument_type="equity_kr",
            forecast_target={
                "kind": "price_target",
                "direction": "at_or_above",
                "target_price": "2463000",
            },
            probability=Decimal("0.55"),
            horizon="10 trading days",
            review_date=date(2026, 7, 17),
            status="open",
        )
    )
    await db_session.flush()

    ctx = await build_decision_context(db_session, symbol=raw, market="kr")

    assert ctx is not None
    assert len(ctx["recent_fills"]) == 1
    fill = ctx["recent_fills"][0]
    assert fill["side"] == "buy"
    assert fill["avg_fill_price"] == 2000000.0
    assert fill["stop_loss"] == 1888000.0
    assert fill["source"] == "kis"
    assert len(ctx["open_claims"]) == 1
    claim = ctx["open_claims"][0]
    assert claim["direction"] == "at_or_above"
    assert claim["target_price"] == "2463000"
    assert claim["review_date"] == "2026-07-17"


@pytest.mark.asyncio
async def test_brier_insufficient_sample_and_empty_returns_none(
    db_session: AsyncSession,
) -> None:
    raw = _uniq_symbol()
    sym = _normalize_symbol_for_filter(raw, "equity_kr")
    # symbol with a decision but zero scored forecasts → insufficient_sample
    report = await _make_report(db_session)
    await _add_item(db_session, report.id, symbol=sym, rationale="real")
    await db_session.flush()

    ctx = await build_decision_context(db_session, symbol=raw, market="kr")
    assert ctx is not None
    # symbol-scoped Brier is deterministic (unique symbol → no forecasts)
    assert ctx["running_brier_symbol"] == {
        "n": 0,
        "mean_brier": None,
        "flag": "insufficient_sample",
    }
    # global Brier aggregates ALL closed+scored forecasts DB-wide; other suites'
    # committed rows leak in under xdist, so assert STRUCTURE only, not the flag.
    g = ctx["running_brier_global"]
    assert set(g.keys()) == {"n", "mean_brier", "flag"}
    assert isinstance(g["n"], int)

    # a symbol with no history anywhere → None (nothing to inject)
    empty = await build_decision_context(db_session, symbol=_uniq_symbol(), market="kr")
    assert empty is None

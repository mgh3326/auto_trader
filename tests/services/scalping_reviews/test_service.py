"""ROB-315 Phase 1 — ScalpingReviewService (rollup draft + operator edits).

Uses the real test DB (db_session). Inserts raw scalp_trade_analytics rows,
then exercises draft build / idempotency / operator-only edits / actions.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.crypto_instruments import CryptoInstrument
from app.models.scalp_trade_analytics import ScalpTradeAnalytics
from app.services.scalping_reviews.service import (
    ScalpingReviewError,
    ScalpingReviewService,
)

_DATE = dt.date(2026, 5, 25)
_NOW = dt.datetime(2026, 5, 25, 12, 0, 0, tzinfo=dt.UTC)


async def _instrument(session, symbol="RVWXRPUSDT") -> int:
    """Get-or-create — crypto_instruments persists across tests in the shared
    test DB, so a fixed insert would collide on its unique key."""
    existing = await session.scalar(
        select(CryptoInstrument).where(
            CryptoInstrument.venue == "binance",
            CryptoInstrument.product == "usdm_futures",
            CryptoInstrument.venue_symbol == symbol,
        )
    )
    if existing is not None:
        return existing.id
    inst = CryptoInstrument(
        venue="binance",
        product="usdm_futures",
        venue_symbol=symbol,
        base_asset="XRP",
        quote_asset="USDT",
        status="active",
    )
    session.add(inst)
    await session.flush()
    return inst.id


async def _analytics(session, instrument_id, *, created_at=_NOW, **kw):
    base = {
        "open_client_order_id": f"o-{kw.get('tag', '0')}",
        "instrument_id": instrument_id,
        "product": "usdm_futures",
        "symbol": "XRPUSDT",
        "side": "BUY",
        "qty": Decimal("1"),
        "created_at": created_at,
        "updated_at": created_at,
    }
    kw.pop("tag", None)
    base.update(kw)
    row = ScalpTradeAnalytics(**base)
    session.add(row)
    await session.flush()
    return row


@pytest.mark.asyncio
async def test_build_draft_rolls_up_analytics(db_session) -> None:
    iid = await _instrument(db_session)
    await _analytics(
        db_session,
        iid,
        tag="win",
        entry_price=Decimal("100"),
        exit_price=Decimal("101"),
        entry_notional_usdt=Decimal("100"),
        net_pnl_usdt=Decimal("0.9"),
        gross_pnl_usdt=Decimal("1.0"),
        exit_reason="take_profit",
    )
    await _analytics(
        db_session,
        iid,
        tag="loss",
        entry_price=Decimal("100"),
        exit_price=Decimal("99"),
        entry_notional_usdt=Decimal("100"),
        net_pnl_usdt=Decimal("-1.1"),
        gross_pnl_usdt=Decimal("-1.0"),
        exit_reason="stop_loss",
    )

    svc = ScalpingReviewService(db_session)
    review = await svc.build_draft(review_date=_DATE, product="usdm_futures", now=_NOW)
    assert review.status == "draft"
    assert review.decision == "review"
    assert review.account_scope == "binance_demo"
    assert review.session_tag == ""
    assert review.trade_count == 2
    assert review.win_count == 1
    assert review.loss_count == 1
    assert review.anomaly_count == 0
    assert review.net_pnl_usdt == Decimal("-0.2")
    assert review.exit_reason_counts == {"take_profit": 1, "stop_loss": 1}
    assert review.source_payload["row_count"] == 2


@pytest.mark.asyncio
async def test_build_draft_is_idempotent_for_default_session_tag(db_session) -> None:
    iid = await _instrument(db_session)
    await _analytics(
        db_session,
        iid,
        tag="a",
        entry_price=Decimal("100"),
        exit_price=Decimal("101"),
        entry_notional_usdt=Decimal("100"),
        net_pnl_usdt=Decimal("0.5"),
        gross_pnl_usdt=Decimal("0.6"),
        exit_reason="take_profit",
    )
    svc = ScalpingReviewService(db_session)
    first = await svc.build_draft(review_date=_DATE, product="usdm_futures", now=_NOW)
    assert first.trade_count == 1

    # A second analytics row, then re-draft with the SAME (default '') key:
    # same row id (no duplicate), refreshed metrics.
    await _analytics(
        db_session,
        iid,
        tag="b",
        entry_price=Decimal("100"),
        exit_price=Decimal("102"),
        entry_notional_usdt=Decimal("100"),
        net_pnl_usdt=Decimal("1.5"),
        gross_pnl_usdt=Decimal("1.6"),
        exit_reason="take_profit",
    )
    second = await svc.build_draft(review_date=_DATE, product="usdm_futures", now=_NOW)
    assert second.id == first.id  # idempotent — no duplicate row
    assert second.trade_count == 2

    rows = await svc.list_reviews(review_date=_DATE, product="usdm_futures")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_build_draft_rejects_non_demo_scope(db_session) -> None:
    svc = ScalpingReviewService(db_session)
    with pytest.raises(ScalpingReviewError):
        await svc.build_draft(
            review_date=_DATE,
            product="usdm_futures",
            now=_NOW,
            account_scope="kis_overseas_live",
        )


@pytest.mark.asyncio
async def test_update_review_touches_only_operator_fields(db_session) -> None:
    iid = await _instrument(db_session)
    raw = await _analytics(
        db_session,
        iid,
        tag="x",
        entry_price=Decimal("100"),
        exit_price=Decimal("101"),
        entry_notional_usdt=Decimal("100"),
        net_pnl_usdt=Decimal("0.9"),
        gross_pnl_usdt=Decimal("1.0"),
        exit_reason="take_profit",
    )
    svc = ScalpingReviewService(db_session)
    review = await svc.build_draft(review_date=_DATE, product="usdm_futures", now=_NOW)

    updated = await svc.update_review(
        review.id,
        now=_NOW,
        observation="spread widened intraday",
        decision="adjust",
        status="reviewed",
    )
    assert updated is not None
    assert updated.observation == "spread widened intraday"
    assert updated.decision == "adjust"
    assert updated.status == "reviewed"
    # Metrics untouched by an operator edit.
    assert updated.trade_count == 1
    assert updated.net_pnl_usdt == Decimal("0.9")
    # Raw analytics row untouched.
    await db_session.refresh(raw)
    assert raw.entry_price == Decimal("100")
    assert raw.net_pnl_usdt == Decimal("0.9")

    with pytest.raises(ScalpingReviewError):
        await svc.update_review(review.id, now=_NOW, decision="bogus")


@pytest.mark.asyncio
async def test_locked_review_is_not_refreshed(db_session) -> None:
    iid = await _instrument(db_session)
    await _analytics(
        db_session,
        iid,
        tag="a",
        entry_price=Decimal("100"),
        exit_price=Decimal("101"),
        entry_notional_usdt=Decimal("100"),
        net_pnl_usdt=Decimal("0.5"),
        gross_pnl_usdt=Decimal("0.6"),
        exit_reason="take_profit",
    )
    svc = ScalpingReviewService(db_session)
    review = await svc.build_draft(review_date=_DATE, product="usdm_futures", now=_NOW)
    await svc.update_review(review.id, now=_NOW, status="locked")

    await _analytics(
        db_session,
        iid,
        tag="b",
        entry_price=Decimal("100"),
        exit_price=Decimal("102"),
        entry_notional_usdt=Decimal("100"),
        net_pnl_usdt=Decimal("1.0"),
        gross_pnl_usdt=Decimal("1.1"),
        exit_reason="take_profit",
    )
    again = await svc.build_draft(review_date=_DATE, product="usdm_futures", now=_NOW)
    assert again.status == "locked"
    assert again.trade_count == 1  # NOT refreshed while locked


@pytest.mark.asyncio
async def test_actions_crud(db_session) -> None:
    iid = await _instrument(db_session)
    await _analytics(
        db_session,
        iid,
        tag="a",
        entry_price=Decimal("100"),
        exit_price=Decimal("101"),
        entry_notional_usdt=Decimal("100"),
        net_pnl_usdt=Decimal("0.5"),
        gross_pnl_usdt=Decimal("0.6"),
        exit_reason="take_profit",
    )
    svc = ScalpingReviewService(db_session)
    review = await svc.build_draft(review_date=_DATE, product="usdm_futures", now=_NOW)

    action = await svc.add_action(
        review.id,
        action_type="parameter_change",
        title="widen TP to 40 bps",
        now=_NOW,
        rationale="MFE consistently exceeds 30 bps",
    )
    assert action.status == "open"
    actions = await svc.list_actions(review.id)
    assert [a.id for a in actions] == [action.id]

    updated = await svc.update_action(action.id, now=_NOW, status="applied")
    assert updated is not None and updated.status == "applied"

    with pytest.raises(ScalpingReviewError):
        await svc.add_action(review.id, action_type="bogus", title="x", now=_NOW)
    with pytest.raises(ScalpingReviewError):
        await svc.update_action(action.id, now=_NOW, status="bogus")


@pytest.mark.asyncio
async def test_set_benchmark_persists_value_and_detail(db_session) -> None:
    iid = await _instrument(db_session)
    await _analytics(
        db_session,
        iid,
        tag="w",
        entry_price=Decimal("100"),
        exit_price=Decimal("101"),
        entry_notional_usdt=Decimal("100"),
        net_pnl_usdt=Decimal("0.9"),
        gross_pnl_usdt=Decimal("1.0"),
        exit_reason="take_profit",
    )
    svc = ScalpingReviewService(db_session)
    await svc.build_draft(review_date=_DATE, product="usdm_futures", now=_NOW)
    updated = await svc.set_benchmark(
        review_date=_DATE,
        product="usdm_futures",
        value=Decimal("12.5"),
        now=_NOW,
        detail={"XRPUSDT": {"open": "100", "close": "100.5", "bps": "50"}},
    )
    assert updated is not None
    assert updated.benchmark_return_bps == Decimal("12.5")
    assert updated.source_payload["benchmark"]["XRPUSDT"]["bps"] == "50"
    assert updated.net_pnl_usdt == Decimal("0.9")  # rollup metrics preserved


@pytest.mark.asyncio
async def test_set_benchmark_noop_on_missing_review(db_session) -> None:
    svc = ScalpingReviewService(db_session)
    assert (
        await svc.set_benchmark(
            review_date=dt.date(2099, 1, 1),
            product="usdm_futures",
            value=Decimal("1"),
            now=_NOW,
        )
        is None
    )


@pytest.mark.asyncio
async def test_set_benchmark_skips_locked_review(db_session) -> None:
    iid = await _instrument(db_session)
    await _analytics(
        db_session,
        iid,
        tag="a",
        entry_price=Decimal("100"),
        exit_price=Decimal("101"),
        entry_notional_usdt=Decimal("100"),
        net_pnl_usdt=Decimal("0.5"),
        gross_pnl_usdt=Decimal("0.6"),
        exit_reason="take_profit",
    )
    svc = ScalpingReviewService(db_session)
    r = await svc.build_draft(review_date=_DATE, product="usdm_futures", now=_NOW)
    await svc.update_review(r.id, now=_NOW, status="locked")
    res = await svc.set_benchmark(
        review_date=_DATE, product="usdm_futures", value=Decimal("9"), now=_NOW
    )
    assert res is not None and res.status == "locked"
    assert res.benchmark_return_bps is None  # not written while locked

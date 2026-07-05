import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeRetrospective
from app.services.trade_journal.aggregates import ClosedTrade, resolve_setup_tag
from app.services.trade_journal.forecast_service import _normalize_symbol_for_filter


def _trade(**kw):
    base = {
        "market": "kr",
        "symbol": "005930",
        "account": "acct",
        "qty": 10,
        "entry_price": 100.0,
        "exit_price": 110.0,
        "entry_ts": datetime(2026, 6, 1, tzinfo=UTC),
        "exit_ts": datetime(2026, 6, 5, tzinfo=UTC),
        "pnl_abs": 100.0,
        "pnl_pct": 0.1,
        "fees": 0.0,
        "entry_item_uuids": (),
        "exit_item_uuid": None,
        "entry_correlation_ids": (),
        "exit_correlation_id": None,
    }
    base.update(kw)
    return ClosedTrade(**base)


def _digit_symbol() -> str:
    """Per-test unique 6-12 digit symbol stable across KR normalizers."""
    return ("9" + uuid.uuid4().hex[:9])[:10].upper()


@pytest.mark.asyncio
async def test_strategy_key_symbol_window(db_session: AsyncSession):
    sym = _digit_symbol()
    norm = _normalize_symbol_for_filter(sym, "equity_kr")
    db_session.add(
        TradeRetrospective(
            symbol=norm,
            instrument_type="equity_kr",
            account_mode="kis_mock",
            outcome="filled",
            strategy_key="pullback_long",
            created_at=datetime(2026, 6, 4, tzinfo=UTC),
        )
    )
    await db_session.flush()

    info = await resolve_setup_tag(db_session, _trade(symbol=sym))
    assert info.tag == "pullback_long"
    assert info.tag_source == "strategy_key"
    assert info.link_quality == "symbol_window"


@pytest.mark.asyncio
async def test_untagged_when_no_signal(db_session: AsyncSession):
    sym = _digit_symbol()
    info = await resolve_setup_tag(db_session, _trade(symbol=sym))
    assert info.tag == "untagged"
    assert info.tag_source == "untagged"
    assert info.link_quality == "symbol_window"

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.core.timezone import now_kst
from app.mcp_server.tooling.trade_journal_tools import compute_active_dca_daily_burn
from app.models.trade_journal import JournalStatus, TradeJournal
from app.models.trading import InstrumentType
from app.services.n8n_daily_brief_service import _build_brief_text


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type: JSONB, _compiler, **_kwargs) -> str:
    return "JSON"


@pytest_asyncio.fixture
async def sqlite_session_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async with engine.begin() as conn:
        await conn.exec_driver_sql("ATTACH DATABASE ':memory:' AS review")
        await conn.run_sync(TradeJournal.__table__.create)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _insert_journals(
    factory: async_sessionmaker[AsyncSession],
    journals: list[TradeJournal],
) -> None:
    async with factory() as session:
        session.add_all(journals)
        await session.commit()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compute_active_dca_daily_burn_sums_mixed_strategies(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = now_kst()
    await _insert_journals(
        sqlite_session_factory,
        [
            TradeJournal(
                id=1,
                symbol="KRW-BTC",
                instrument_type=InstrumentType.crypto,
                side="buy",
                thesis="dca",
                status=JournalStatus.active,
                strategy="dca_oversold",
                amount=Decimal("90000"),
                min_hold_days=9,
                hold_until=now + timedelta(days=7),
            ),
            TradeJournal(
                id=2,
                symbol="KRW-ETH",
                instrument_type=InstrumentType.crypto,
                side="buy",
                thesis="dca",
                status=JournalStatus.active,
                strategy="coinmoogi",
                extra_metadata={"amount_krw": 200000, "hold_days": 10},
                hold_until=now + timedelta(days=3),
            ),
            TradeJournal(
                id=3,
                symbol="KRW-XRP",
                instrument_type=InstrumentType.crypto,
                side="buy",
                thesis="dca",
                status=JournalStatus.active,
                strategy="dca_oversold",
                extra_metadata={"amount_krw": 600000, "hold_days": 20},
                hold_until=now + timedelta(days=10),
            ),
        ],
    )

    monkeypatch.setattr(
        "app.mcp_server.tooling.trade_journal_tools._session_factory",
        lambda: sqlite_session_factory,
    )

    result = await compute_active_dca_daily_burn()

    assert result["active_count"] == 3
    assert result["daily_burn_krw"] == pytest.approx(60000.0)
    assert result["days_to_next_obligation"] == 3
    assert result["cash_needed_until_obligation"] == pytest.approx(180000.0)
    assert len(result["per_record"]) == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compute_active_dca_daily_burn_excludes_closed_status(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = now_kst()
    await _insert_journals(
        sqlite_session_factory,
        [
            TradeJournal(
                id=11,
                symbol="KRW-BTC",
                instrument_type=InstrumentType.crypto,
                side="buy",
                thesis="dca",
                status=JournalStatus.active,
                strategy="dca_oversold",
                amount=Decimal("90000"),
                min_hold_days=9,
                hold_until=now + timedelta(days=4),
            ),
            TradeJournal(
                id=12,
                symbol="KRW-ETH",
                instrument_type=InstrumentType.crypto,
                side="buy",
                thesis="dca",
                status=JournalStatus.closed,
                strategy="coinmoogi",
                extra_metadata={"amount_krw": 200000, "hold_days": 10},
                hold_until=now + timedelta(days=3),
            ),
        ],
    )

    monkeypatch.setattr(
        "app.mcp_server.tooling.trade_journal_tools._session_factory",
        lambda: sqlite_session_factory,
    )

    result = await compute_active_dca_daily_burn()

    assert result["active_count"] == 1
    assert result["daily_burn_krw"] == pytest.approx(10000.0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compute_active_dca_daily_burn_returns_zero_when_no_active(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _insert_journals(
        sqlite_session_factory,
        [
            TradeJournal(
                id=21,
                symbol="KRW-BTC",
                instrument_type=InstrumentType.crypto,
                side="buy",
                thesis="dca",
                status=JournalStatus.closed,
                strategy="dca_oversold",
                amount=Decimal("90000"),
                min_hold_days=9,
            )
        ],
    )

    monkeypatch.setattr(
        "app.mcp_server.tooling.trade_journal_tools._session_factory",
        lambda: sqlite_session_factory,
    )

    result = await compute_active_dca_daily_burn()

    assert result["daily_burn_krw"] == 0.0
    assert result["active_count"] == 0
    assert result["days_to_next_obligation"] is None
    assert result["cash_needed_until_obligation"] == 0.0
    assert result["per_record"] == []


@pytest.mark.unit
def test_brief_text_renders_recomputed_daily_burn_line() -> None:
    text = _build_brief_text(
        date_fmt="03/17 (화)",
        market_overview={
            "fear_greed": None,
            "btc_dominance": None,
            "total_market_cap_change_24h": None,
            "economic_events_today": [],
        },
        pending_by_market={},
        portfolio_by_market={},
        yesterday_fills={"total": 0, "fills": []},
        daily_burn={"daily_burn_krw": 60000.0, "active_count": 3},
    )

    assert "daily_burn: 60,000 KRW (active DCA 3종 · 재산출)" in text

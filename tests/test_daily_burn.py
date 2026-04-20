from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.core.timezone import KST, now_kst
from app.mcp_server.tooling.trade_journal_tools import (
    _extract_daily_allocation_krw,
    compute_active_dca_daily_burn,
)
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
    now = datetime(2026, 4, 17, 12, 0, tzinfo=KST)
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
    monkeypatch.setattr(
        "app.mcp_server.tooling.trade_journal_tools.now_kst",
        lambda: now,
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

    assert result["daily_burn_krw"] == pytest.approx(0.0)
    assert result["active_count"] == 0
    assert result["days_to_next_obligation"] is None
    assert result["cash_needed_until_obligation"] == pytest.approx(0.0)
    assert result["per_record"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compute_active_dca_daily_burn_uses_kst_date_for_utc_hold_until(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 4, 17, 0, 0, tzinfo=KST)
    # 2026-04-17 15:30 UTC == 2026-04-18 00:30 KST
    hold_until_utc = datetime(2026, 4, 17, 15, 30, tzinfo=UTC)

    await _insert_journals(
        sqlite_session_factory,
        [
            TradeJournal(
                id=31,
                symbol="KRW-BTC",
                instrument_type=InstrumentType.crypto,
                side="buy",
                thesis="dca",
                status=JournalStatus.active,
                strategy="dca_oversold",
                amount=Decimal("90000"),
                min_hold_days=9,
                hold_until=hold_until_utc,
            )
        ],
    )

    monkeypatch.setattr(
        "app.mcp_server.tooling.trade_journal_tools._session_factory",
        lambda: sqlite_session_factory,
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.trade_journal_tools.now_kst",
        lambda: fixed_now,
    )

    result = await compute_active_dca_daily_burn()

    assert result["days_to_next_obligation"] == 1
    assert result["cash_needed_until_obligation"] == pytest.approx(10000.0)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("extra_metadata", "amount", "min_hold_days", "expected"),
    [
        ({"amount_krw": 100000, "hold_days": 10}, None, None, 10000.0),
        ({"amount_krw": 120000}, Decimal("60000"), 6, 20000.0),
        ({}, Decimal("60000"), 6, 10000.0),
        ({"amount_krw": "abc", "hold_days": 10}, None, None, 0.0),
        ({"amount_krw": 100000, "hold_days": 0}, None, None, 0.0),
        ({"amount_krw": -100000, "hold_days": 10}, None, None, 0.0),
    ],
)
def test_extract_daily_allocation_krw_edge_cases(
    extra_metadata: dict[str, int | str],
    amount: Decimal | None,
    min_hold_days: int | None,
    expected: float,
) -> None:
    journal = TradeJournal(
        id=41,
        symbol="KRW-BTC",
        instrument_type=InstrumentType.crypto,
        side="buy",
        thesis="dca",
        status=JournalStatus.active,
        strategy="dca_oversold",
        extra_metadata=extra_metadata,
        amount=amount,
        min_hold_days=min_hold_days,
    )

    assert _extract_daily_allocation_krw(journal) == pytest.approx(expected)


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


@pytest.mark.unit
def test_brief_text_marks_daily_burn_unavailable_when_recompute_failed() -> None:
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
        daily_burn={
            "daily_burn_krw": 0.0,
            "active_count": 0,
            "error": "compute_active_dca_daily_burn failed: db unavailable",
        },
    )

    assert "daily_burn: unavailable" in text
    assert "daily_burn: 0 KRW" not in text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_daily_brief_marks_daily_burn_unavailable_when_task_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise_daily_burn() -> dict[str, object]:
        raise RuntimeError("db unavailable")

    async def _fetch_pending_orders(**_kwargs: object) -> dict[str, object]:
        return {"orders": [], "errors": []}

    async def _get_portfolio_overview(_markets: list[str]) -> dict[str, object]:
        return {"positions": [], "warnings": []}

    async def _fetch_market_context(**_kwargs: object) -> dict[str, object]:
        return {
            "market_overview": {
                "fear_greed": None,
                "btc_dominance": None,
                "total_market_cap_change_24h": None,
                "economic_events_today": [],
            },
            "errors": [],
        }

    async def _fetch_yesterday_fills(**_kwargs: object) -> dict[str, object]:
        return {"total": 0, "fills": []}

    monkeypatch.setattr(
        "app.services.n8n_daily_brief_service.compute_active_dca_daily_burn",
        _raise_daily_burn,
    )
    monkeypatch.setattr(
        "app.services.n8n_daily_brief_service.fetch_pending_orders",
        _fetch_pending_orders,
    )
    monkeypatch.setattr(
        "app.services.n8n_daily_brief_service._get_portfolio_overview",
        _get_portfolio_overview,
    )
    monkeypatch.setattr(
        "app.services.n8n_daily_brief_service.fetch_market_context",
        _fetch_market_context,
    )
    monkeypatch.setattr(
        "app.services.n8n_daily_brief_service._fetch_yesterday_fills",
        _fetch_yesterday_fills,
    )

    from app.services.n8n_daily_brief_service import fetch_daily_brief

    result = await fetch_daily_brief(markets=["crypto"])

    assert result["daily_burn"]["error"] == "db unavailable"
    assert {"source": "daily_burn", "error": "db unavailable"} in result["errors"]
    assert "daily_burn: unavailable" in result["brief_text"]
    assert "daily_burn: 0 KRW" not in result["brief_text"]

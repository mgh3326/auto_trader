"""ROB-928 — downside watch auto-register + stop_loss->watch mirror.

Covers: stop_loss mirror level selection (max across lots), 20d-low
fallback when no journal has a stop_loss, idempotent skip of an
already-active below+defensive watch, and dry_run defaulting to zero DB
mutation. Alert-table cleanup is handled by the shared ``session``
fixture (tests/_investment_reports_helpers.py truncates
review.investment_watch_alerts between tests); trade_journals rows are
seeded with unique per-test symbols and cleaned up explicitly since that
table is outside that fixture's truncation list.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentWatchAlert
from app.models.trade_journal import TradeJournal
from app.services.downside_watch_service import DownsideWatchService


def _symbol() -> str:
    return f"R928{uuid.uuid4().hex[:6].upper()}"


async def _price_above_stop(_symbol: str) -> Decimal:
    return Decimal("999999")


async def _seed_journal(
    session: AsyncSession,
    *,
    symbol: str,
    quantity: str = "10",
    entry_price: str = "50000",
    stop_loss: str | None = None,
    status: str = "active",
    account_type: str = "live",
    side: str = "buy",
) -> TradeJournal:
    journal = TradeJournal(
        symbol=symbol,
        instrument_type="equity_kr",
        side=side,
        entry_price=Decimal(entry_price),
        quantity=Decimal(quantity),
        thesis="ROB-928 test fixture",
        stop_loss=Decimal(stop_loss) if stop_loss is not None else None,
        status=status,
        account_type=account_type,
    )
    session.add(journal)
    await session.flush()
    return journal


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_trade_journals(session: AsyncSession):
    yield
    await session.execute(
        sa.delete(TradeJournal).where(TradeJournal.symbol.like("R928%"))
    )
    await session.commit()


@pytest.mark.asyncio
async def test_compute_levels_prefers_stop_loss_mirror_max_across_lots(
    session: AsyncSession,
) -> None:
    symbol = _symbol()
    await _seed_journal(session, symbol=symbol, quantity="5", stop_loss="48000")
    await _seed_journal(session, symbol=symbol, quantity="5", stop_loss="49500")
    await session.commit()

    async def _fail_recent_low(_symbol: str) -> Decimal | None:
        raise AssertionError(
            "recent-low fallback must not be called when stop_loss exists"
        )

    service = DownsideWatchService(
        session,
        recent_low_fetcher=_fail_recent_low,
        current_price_fetcher=_price_above_stop,
    )
    levels = await service.compute_levels()

    matching = [lvl for lvl in levels if lvl.symbol == symbol]
    assert len(matching) == 1
    level = matching[0]
    assert level.source == "stop_loss_mirror"
    assert level.threshold == Decimal("49500")
    assert level.quantity == Decimal("10")


@pytest.mark.asyncio
async def test_compute_levels_falls_back_to_recent_low_when_no_stop_loss(
    session: AsyncSession,
) -> None:
    symbol = _symbol()
    await _seed_journal(session, symbol=symbol, quantity="3", stop_loss=None)
    await session.commit()

    async def _fake_recent_low(sym: str) -> Decimal | None:
        assert sym == symbol
        return Decimal("46250")

    service = DownsideWatchService(session, recent_low_fetcher=_fake_recent_low)
    levels = await service.compute_levels()

    matching = [lvl for lvl in levels if lvl.symbol == symbol]
    assert len(matching) == 1
    level = matching[0]
    assert level.source == "recent_low_20d"
    assert level.threshold == Decimal("46250")


@pytest.mark.asyncio
async def test_compute_levels_skips_symbol_when_no_stop_loss_and_no_recent_low_data(
    session: AsyncSession,
) -> None:
    symbol = _symbol()
    await _seed_journal(session, symbol=symbol, stop_loss=None)
    await session.commit()

    async def _no_data(_symbol: str) -> Decimal | None:
        return None

    service = DownsideWatchService(session, recent_low_fetcher=_no_data)
    levels = await service.compute_levels()

    assert all(lvl.symbol != symbol for lvl in levels)


@pytest.mark.asyncio
async def test_compute_levels_ignores_non_kr_equity_and_non_active_holdings(
    session: AsyncSession,
) -> None:
    symbol_draft = _symbol()
    symbol_us = _symbol()
    await _seed_journal(session, symbol=symbol_draft, stop_loss="1000", status="draft")
    journal_us = TradeJournal(
        symbol=symbol_us,
        instrument_type="equity_us",
        side="buy",
        entry_price=Decimal("100"),
        quantity=Decimal("1"),
        thesis="ROB-928 test fixture (US, excluded)",
        stop_loss=Decimal("90"),
        status="active",
        account_type="live",
    )
    session.add(journal_us)
    await session.commit()

    async def _no_recent_low(_symbol: str) -> Decimal | None:
        return None

    service = DownsideWatchService(session, recent_low_fetcher=_no_recent_low)
    levels = await service.compute_levels()

    symbols = {lvl.symbol for lvl in levels}
    assert symbol_draft not in symbols
    assert symbol_us not in symbols


@pytest.mark.asyncio
async def test_register_sweep_skips_symbol_with_existing_active_downside_watch(
    session: AsyncSession,
) -> None:
    symbol = _symbol()
    await _seed_journal(session, symbol=symbol, stop_loss="48000")
    await session.commit()

    async def _fail_recent_low(_symbol: str) -> Decimal | None:
        raise AssertionError("must not be called; stop_loss present")

    service = DownsideWatchService(
        session,
        recent_low_fetcher=_fail_recent_low,
        current_price_fetcher=_price_above_stop,
    )

    first = await service.register_sweep(dry_run=False)
    assert len(first["registered"]) == 1
    assert first["registered"][0]["symbol"] == symbol

    second = await service.register_sweep(dry_run=False)
    assert all(entry["symbol"] != symbol for entry in second["registered"])
    assert any(
        entry["symbol"] == symbol and entry["reason"] == "active_downside_watch_exists"
        for entry in second["skipped_existing"]
    )

    count = await session.scalar(
        sa.select(sa.func.count())
        .select_from(InvestmentWatchAlert)
        .where(InvestmentWatchAlert.symbol == symbol)
    )
    assert count == 1


@pytest.mark.asyncio
async def test_register_sweep_dry_run_default_makes_no_db_changes(
    session: AsyncSession,
) -> None:
    symbol = _symbol()
    await _seed_journal(session, symbol=symbol, stop_loss="48000")
    await session.commit()

    service = DownsideWatchService(session, current_price_fetcher=_price_above_stop)
    result = await service.register_sweep()

    assert result["dry_run"] is True
    assert len(result["registered"]) == 1
    assert result["registered"][0]["would_register"] is True

    count = await session.scalar(
        sa.select(sa.func.count()).select_from(InvestmentWatchAlert)
    )
    assert count == 0


@pytest.mark.asyncio
async def test_register_sweep_registered_alert_shape(session: AsyncSession) -> None:
    symbol = _symbol()
    await _seed_journal(session, symbol=symbol, stop_loss="48000")
    await session.commit()

    service = DownsideWatchService(session, current_price_fetcher=_price_above_stop)
    await service.register_sweep(dry_run=False)

    alert = await session.scalar(
        sa.select(InvestmentWatchAlert).where(InvestmentWatchAlert.symbol == symbol)
    )
    assert alert is not None
    assert alert.market == "kr"
    assert alert.metric == "price"
    assert alert.operator == "below"
    assert alert.intent in {"sell_review", "risk_review"}
    assert alert.threshold == Decimal("48000")
    assert alert.action_mode == "notify_only"
    assert alert.status == "active"
    assert "stop_loss_mirror" in alert.rationale


@pytest.mark.asyncio
async def test_register_sweep_registers_and_tags_already_breached_watch(
    session: AsyncSession,
) -> None:
    """ROB-971 HIGH-1 fix: a below watch whose price has already crossed its
    threshold must still be registered (never silently dropped — that was
    the one case where the most at-risk holding lost monitoring). It's
    tagged instead with the registration-time price/threshold as a fact,
    not an interpretation of what the eventual fire means."""
    symbol = _symbol()
    await _seed_journal(session, symbol=symbol, stop_loss="48000")
    await session.commit()

    async def _already_breached(_symbol: str) -> Decimal:
        return Decimal("47999")

    result = await DownsideWatchService(
        session, current_price_fetcher=_already_breached
    ).register_sweep(dry_run=False)

    assert len(result["registered"]) == 1
    entry = result["registered"][0]
    assert entry["symbol"] == symbol
    assert entry["already_breached"] is True
    # Kept for response-shape backward compatibility; must stay empty now
    # that already-breached levels are registered rather than skipped.
    assert result["skipped_already_triggered"] == []

    alert = await session.scalar(
        sa.select(InvestmentWatchAlert).where(InvestmentWatchAlert.symbol == symbol)
    )
    assert alert is not None
    assert alert.status == "active"
    tag_metadata = alert.alert_metadata["rob971_already_breached"]
    assert tag_metadata["reason"] == "already_breached"
    assert Decimal(tag_metadata["current_price_at_registration"]) == Decimal("47999")
    assert Decimal(tag_metadata["threshold"]) == Decimal("48000")

    checklist_entry = next(
        entry for entry in alert.trigger_checklist if "already_breached" in entry
    )
    # Must carry the registration-time price and threshold as facts, not
    # just the bare "already_breached" marker (a truncated tag phrase would
    # still say "already_breached" but drop the evidence an operator needs
    # to judge the alert — this must fail if that evidence is dropped).
    assert "47999" in checklist_entry
    assert "48000" in checklist_entry


@pytest.mark.asyncio
async def test_register_sweep_boundary_at_exact_threshold_is_not_tagged_breached(
    session: AsyncSession,
) -> None:
    """MEDIUM-2: the already_breached tag must align with the below-scanner's
    strict ``<`` (app.jobs.watch_market_data.is_triggered) — a price exactly
    at the threshold has not yet triggered, so it registers untagged."""
    symbol = _symbol()
    await _seed_journal(session, symbol=symbol, stop_loss="48000")
    await session.commit()

    async def _exactly_at_threshold(_symbol: str) -> Decimal:
        return Decimal("48000")

    result = await DownsideWatchService(
        session, current_price_fetcher=_exactly_at_threshold
    ).register_sweep(dry_run=False)

    assert len(result["registered"]) == 1
    assert result["registered"][0]["already_breached"] is False

    alert = await session.scalar(
        sa.select(InvestmentWatchAlert).where(InvestmentWatchAlert.symbol == symbol)
    )
    assert alert is not None
    assert "rob971_already_breached" not in alert.alert_metadata
    assert alert.trigger_checklist == []


@pytest.mark.asyncio
async def test_register_sweep_price_unavailable_is_reported_separately(
    session: AsyncSession,
) -> None:
    """LOW-3: registration-time price failures are a distinct bucket from
    the (now-unused) already-triggered skip bucket — the two mean opposite
    things and must not be conflated."""
    symbol = _symbol()
    await _seed_journal(session, symbol=symbol, stop_loss="48000")
    await session.commit()

    async def _unavailable(_symbol: str) -> Decimal | None:
        return None

    result = await DownsideWatchService(
        session, current_price_fetcher=_unavailable
    ).register_sweep(dry_run=False)

    assert result["registered"] == []
    assert result["skipped_already_triggered"] == []
    assert result["skipped_price_unavailable"] == [
        {"symbol": symbol, "reason": "current_price_unavailable"}
    ]

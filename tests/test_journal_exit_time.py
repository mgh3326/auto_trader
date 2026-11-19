from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp_server.tooling import order_journal
from app.models.trade_journal import TradeJournal
from app.models.trading import InstrumentType


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", [1, 2])
async def test_close_uses_fill_time_and_krw_pnl(quantity):
    at = datetime(2026, 11, 18, 7, 14, tzinfo=UTC)
    lot = TradeJournal(
        id=1,
        symbol="041510",
        instrument_type=InstrumentType.equity_kr,
        side="buy",
        status="active",
        entry_price=Decimal("82200"),
        quantity=Decimal(quantity),
        created_at=at,
    )
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [lot]
    db.execute.return_value = result
    db.add = MagicMock()
    await order_journal._close_journals_on_sell(
        symbol=lot.symbol, sell_quantity=1, sell_price=91000, exit_at=at, db=db
    )
    closed = lot if quantity == 1 else db.add.call_args.args[0]
    assert closed.exit_date == at
    assert closed.total_pnl_krw == Decimal("8800")
    assert closed.pnl_pct == (Decimal("8800") / Decimal("82200")) * 100


@pytest.mark.asyncio
async def test_default_time_and_naive_rejected():
    at = datetime(2026, 11, 19, tzinfo=UTC)
    lot = TradeJournal(
        id=1,
        symbol="041510",
        instrument_type=InstrumentType.equity_kr,
        side="buy",
        status="active",
        entry_price=Decimal("1"),
        quantity=Decimal("1"),
        created_at=at,
    )
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [lot]
    db.execute.return_value = result
    with patch.object(order_journal, "now_kst", return_value=at):
        await order_journal._close_journals_on_sell(
            symbol=lot.symbol, sell_quantity=1, sell_price=2, db=db
        )
    assert lot.exit_date == at
    with pytest.raises(ValueError):
        await order_journal._close_journals_on_sell(
            symbol=lot.symbol,
            sell_quantity=1,
            sell_price=2,
            exit_at=datetime(2026, 1, 1),
            db=db,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "symbol,expected", [("KRW-BTC", Decimal("8800")), ("BTC-USD", None)]
)
async def test_crypto_quote_currency(symbol, expected):
    lot = TradeJournal(
        id=1,
        symbol=symbol,
        instrument_type=InstrumentType.crypto,
        side="buy",
        status="active",
        entry_price=Decimal("82200"),
        quantity=Decimal("1"),
        created_at=datetime.now(UTC),
    )
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [lot]
    db.execute.return_value = result
    await order_journal._close_journals_on_sell(
        symbol=symbol, sell_quantity=1, sell_price=91000, db=db
    )
    assert lot.total_pnl_krw == expected


@pytest.mark.asyncio
async def test_us_fx_split_unchanged():
    lot = TradeJournal(
        id=1,
        symbol="AAPL",
        instrument_type=InstrumentType.equity_us,
        side="buy",
        status="active",
        entry_price=Decimal("100"),
        quantity=Decimal("2"),
        buy_fx_rate=Decimal("1300"),
        created_at=datetime.now(UTC),
    )
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [lot]
    db.execute.return_value = result
    await order_journal._close_journals_on_sell(
        symbol="AAPL", sell_quantity=2, sell_price=110, sell_fx_rate=1400, db=db
    )
    assert lot.security_pnl_usd == Decimal("20")
    assert lot.security_pnl_krw == Decimal("28000")
    assert lot.fx_pnl_krw == Decimal("20000")
    assert lot.total_pnl_krw == Decimal("48000")


@pytest.mark.asyncio
async def test_toss_passes_broker_fill_time():
    from types import SimpleNamespace

    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    at = datetime(2026, 11, 18, 7, 14, 14, tzinfo=UTC)
    row = SimpleNamespace(
        id=1,
        broker_order_id="order",
        client_order_id="client",
        market="kr",
        symbol="041510",
        operation_kind="place",
        status="accepted",
        filled_qty=Decimal("0"),
        side="sell",
        currency="KRW",
        exit_reason=None,
        reason=None,
        journal_id=None,
    )
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("1"),
        avg_price=Decimal("91000"),
        commission=None,
        tax=None,
        fee_total=Decimal("0"),
        settlement_date=None,
        raw_order={"execution": {"filledAt": at.isoformat()}},
        reason="filled",
    )
    source = SimpleNamespace(evidence_for=AsyncMock(return_value=evidence))
    cm = AsyncMock()
    cm.__aenter__.return_value = AsyncMock()
    service = MagicMock()
    service.update_reconcile_outcome = AsyncMock()
    close = AsyncMock(return_value=None)
    with (
        patch.object(mod, "_save_order_fill", AsyncMock(return_value=1)),
        patch.object(mod, "_close_journals_on_sell", close),
        patch.object(mod, "_order_session_factory", return_value=lambda: cm),
        patch.object(mod, "TossLiveOrderLedgerService", return_value=service),
        patch.object(
            mod, "upsert_toss_execution_fill", AsyncMock(return_value=("unchanged", 1))
        ),
        patch.object(mod, "_notify_toss_fill", AsyncMock(return_value=False)),
    ):
        result = await mod._reconcile_one_toss_row(
            row, dry_run=False, evidence_source=source, project_proposal_rungs=False
        )
    assert result["action"] == "booked"
    assert close.await_args.kwargs["exit_at"] == at

"""In-memory digest sources for ROB-1297 unit tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from app.services.market_close_digest.types import LedgerFill, ProposalRow, RetroRow

AC1_SESSION_DATE = date(2026, 8, 19)


def _ts(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 19, hour, minute, tzinfo=UTC)


def ac1_fills() -> tuple[LedgerFill, ...]:
    """08-19 ET expected counts: sell 4, buy 1."""
    sells = (
        LedgerFill(
            source="toss_live_order_ledger",
            broker="toss",
            symbol="AAPL",
            side="sell",
            qty=Decimal("1"),
            price=Decimal("220"),
            notional=Decimal("220"),
            pnl=Decimal("12.30"),
            pnl_pct=Decimal("1.2"),
            pnl_currency="USD",
            filled_at=_ts(14, 10),
        ),
        LedgerFill(
            source="toss_live_order_ledger",
            broker="toss",
            symbol="MSFT",
            side="sell",
            qty=Decimal("1"),
            price=Decimal("410"),
            notional=Decimal("410"),
            pnl=Decimal("-3.10"),
            pnl_pct=Decimal("-0.4"),
            pnl_currency="USD",
            filled_at=_ts(14, 20),
        ),
        LedgerFill(
            source="toss_live_order_ledger",
            broker="toss",
            symbol="NVDA",
            side="sell",
            qty=Decimal("1"),
            price=Decimal("170"),
            notional=Decimal("170"),
            pnl=Decimal("5.00"),
            pnl_pct=Decimal("0.8"),
            pnl_currency="USD",
            filled_at=_ts(15, 0),
        ),
        LedgerFill(
            source="toss_live_order_ledger",
            broker="toss",
            symbol="META",
            side="sell",
            qty=Decimal("1"),
            price=Decimal("500"),
            notional=Decimal("500"),
            pnl=Decimal("8.00"),
            pnl_pct=Decimal("0.5"),
            pnl_currency="USD",
            filled_at=_ts(15, 30),
        ),
    )
    buy = LedgerFill(
        source="toss_live_order_ledger",
        broker="toss",
        symbol="AMD",
        side="buy",
        qty=Decimal("2"),
        price=Decimal("160"),
        notional=Decimal("320"),
        pnl=None,
        pnl_pct=None,
        pnl_currency="USD",
        filled_at=_ts(16, 0),
    )
    return (*sells, buy)


def ac1_oversell_proposals() -> tuple[ProposalRow, ...]:
    return (
        ProposalRow(
            symbol="AMZN",
            side="sell",
            market="equity_us",
            auto_approved=False,
            card_kind="manual",
            lifecycle_state="rejected",
            void_reason="Requested sell quantity 2 exceeds orderable balance 1.",
            created_at=_ts(13, 0),
        ),
        ProposalRow(
            symbol="GOOGL",
            side="sell",
            market="equity_us",
            auto_approved=False,
            card_kind="manual",
            lifecycle_state="rejected",
            void_reason="Requested sell quantity 1 exceeds sellable 0",
            created_at=_ts(13, 5),
        ),
        ProposalRow(
            symbol="AMD",
            side="buy",
            market="equity_us",
            auto_approved=True,
            card_kind=None,
            lifecycle_state="filled",
            void_reason=None,
            created_at=_ts(13, 10),
        ),
    )


class FakeDigestSources:
    def __init__(
        self,
        fills: tuple[LedgerFill, ...] = (),
        proposals: tuple[ProposalRow, ...] = (),
        retros: tuple[RetroRow, ...] = (),
    ) -> None:
        self.fills = fills
        self.proposals = proposals
        self.retros = retros

    async def list_fills(self, market, window_start, window_end):  # noqa: ANN001
        return self.fills

    async def list_proposals(self, market, window_start, window_end):  # noqa: ANN001
        return self.proposals

    async def list_retros(self, market, window_start, window_end):  # noqa: ANN001
        return self.retros

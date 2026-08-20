"""ROB-1297 market-close digest value types.

Pure dataclasses. No DB, network, or notifier imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

Market = Literal["us", "kr", "crypto"]
DigestStatus = Literal[
    "ok",
    "zero_fills",
    "skipped_holiday",
    "aborted_mutation",
    "send_failed",
]

PROPOSAL_MARKET: dict[Market, str] = {
    "us": "equity_us",
    "kr": "equity_kr",
    "crypto": "crypto",
}


@dataclass(frozen=True)
class LedgerFill:
    source: str
    broker: str
    symbol: str
    side: Literal["buy", "sell"]
    qty: Decimal
    price: Decimal | None
    notional: Decimal | None
    pnl: Decimal | None
    pnl_pct: Decimal | None
    pnl_currency: str | None
    filled_at: datetime
    correlation_id: str | None = None


@dataclass(frozen=True)
class ProposalRow:
    symbol: str
    side: Literal["buy", "sell"]
    market: str
    auto_approved: bool
    card_kind: str | None
    lifecycle_state: str
    void_reason: str | None
    created_at: datetime


@dataclass(frozen=True)
class RetroRow:
    symbol: str
    side: str | None
    realized_pnl: Decimal | None
    pnl_pct: Decimal | None
    pnl_currency: str | None
    correlation_id: str | None
    created_at: datetime


@dataclass(frozen=True)
class OversellBlock:
    symbol: str
    reason: str


@dataclass(frozen=True)
class DigestSnapshot:
    market: Market
    session_date: date
    status: DigestStatus
    fills: tuple[LedgerFill, ...] = ()
    oversell_blocked: tuple[OversellBlock, ...] = ()
    auto_approve_count: int = 0
    card_count: int = 0
    flags: tuple[str, ...] = ()
    window_start: datetime | None = None
    window_end: datetime | None = None

    @property
    def fill_count(self) -> int:
        return len(self.fills)

    @property
    def buy_count(self) -> int:
        return sum(1 for fill in self.fills if fill.side == "buy")

    @property
    def sell_count(self) -> int:
        return sum(1 for fill in self.fills if fill.side == "sell")

    @property
    def buys(self) -> tuple[LedgerFill, ...]:
        return tuple(fill for fill in self.fills if fill.side == "buy")

    @property
    def sells(self) -> tuple[LedgerFill, ...]:
        return tuple(fill for fill in self.fills if fill.side == "sell")

    @property
    def net_notional(self) -> Decimal:
        total = Decimal("0")
        for fill in self.fills:
            if fill.notional is None:
                continue
            if fill.side == "buy":
                total += fill.notional
            else:
                total -= fill.notional
        return total


@dataclass
class DigestRunResult:
    market: Market
    session_date: date
    status: DigestStatus
    message: str
    sent: bool
    mutation_count: int
    snapshot: DigestSnapshot | None = None
    extra: dict[str, object] = field(default_factory=dict)

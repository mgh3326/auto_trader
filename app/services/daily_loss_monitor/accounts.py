"""The autonomous-order accounts (6939) and their ledger identity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class AccountScope:
    account: str
    broker: str
    instrument_type: str
    currency: str


ACCOUNTS: Final[dict[str, AccountScope]] = {
    scope.account: scope
    for scope in (
        AccountScope("kis_live_kr", "kis", "equity_kr", "KRW"),
        AccountScope("kis_live_us", "kis", "equity_us", "USD"),
        AccountScope("toss_live_kr", "toss", "equity_kr", "KRW"),
        AccountScope("toss_live_us", "toss", "equity_us", "USD"),
        AccountScope("upbit_live", "upbit", "crypto", "KRW"),
    )
}

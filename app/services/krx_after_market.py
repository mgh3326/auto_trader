"""#925 — KRX after-market (16:00-20:00 KST) per-symbol eligibility.

The KRX after-market opened 2026-09-14 (16:00-20:00 KST, continuous
matching) and covers roughly 2,000 KOSPI/KOSDAQ shares; ETFs/ETNs and other
off-regular products are announced as a later phase, and there is no KRX
pre-market (08:00-08:50 stays NXT-only). Sources: theguru.co.kr 2026-09-27
news no=107383 as summarised in hk:doc
strategy-lab/2026-09-29/krx-aftermarket-vs-nxt (id 8157), plus the 375500
evidence there (nxt_tradable=false yet traded on Toss 15-minute bars
2026-09-28 16:00-19:45).

Authority: the per-symbol list is the KRX-published after-market eligibility
list, imported by an operator into ``krx_after_market_eligibility`` with
``scripts/import_krx_after_market_eligibility.py`` (dry-run default). No
rule over the symbol universe stands in for it: roughly 2,000 names are
eligible out of a larger stock universe, so "is a stock" is not evidence of
eligibility.

Fail-closed: ``krx_after_tradable`` is True only when every positive fact is
present — the symbol is on a non-stale imported list, is a KOSPI/KOSDAQ
``STOCK`` in the trusted universe (ETF/ETN/unknown classification is False
even if listed) and is explicitly not KRX trading-suspended. Anything
unknown is False, never None and never True.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

_KST = dt.timezone(dt.timedelta(hours=9))

KRX_AFTER_MARKET_SOURCE = "krx_after_market_eligibility"
# The list is operator-imported, not auto-synced; an import attests the list
# is current as of ``list_asof``. Past this age the whole list is unknown and
# every symbol reads False.
KRX_AFTER_LIST_STALE_AFTER = dt.timedelta(days=7)

KRX_AFTER_EXCHANGES = frozenset({"KOSPI", "KOSDAQ"})
KRX_AFTER_STOCK_SECURITY_TYPE = "STOCK"
_ETP_SECURITY_TYPES = frozenset({"ETF", "ETN"})

REASON_TRADABLE = "krx_after_tradable"
REASON_LIST_MISSING = "krx_after_list_missing"
REASON_NOT_LISTED = "not_krx_after_listed"
REASON_EXCHANGE_INELIGIBLE = "krx_after_exchange_ineligible"
REASON_SECURITY_TYPE_UNKNOWN = "security_type_unknown"
REASON_ETP_EXCLUDED = "etf_etn_excluded"
REASON_NON_STOCK_EXCLUDED = "non_stock_excluded"
REASON_KRX_SUSPENDED = "krx_trading_suspended"
REASON_KRX_SUSPENDED_UNKNOWN = "krx_trading_suspended_unknown"
REASON_STALE_ASOF = "stale_asof"
REASON_MISSING_ASOF = "missing_asof"
REASON_NOT_IN_UNIVERSE = "not_in_active_universe"
REASON_LOOKUP_FAILED = "lookup_failed"


@dataclass(frozen=True)
class KrxAfterTradability:
    """Positive-evidence KRX after-market capability for one active symbol."""

    listed: bool | None  # None = no list imported at all
    exchange: str | None
    security_type: str | None
    krx_trading_suspended: bool | None
    asof: dt.datetime | None
    list_source: str | None = None
    source: str = KRX_AFTER_MARKET_SOURCE

    @property
    def reason(self) -> str:
        if self.listed is None:
            return REASON_LIST_MISSING
        if self.listed is not True:
            return REASON_NOT_LISTED
        if (self.exchange or "").strip().upper() not in KRX_AFTER_EXCHANGES:
            return REASON_EXCHANGE_INELIGIBLE
        security_type = (
            self.security_type.strip().upper()
            if isinstance(self.security_type, str)
            else ""
        )
        if not security_type:
            return REASON_SECURITY_TYPE_UNKNOWN
        if security_type in _ETP_SECURITY_TYPES:
            return REASON_ETP_EXCLUDED
        if security_type != KRX_AFTER_STOCK_SECURITY_TYPE:
            return REASON_NON_STOCK_EXCLUDED
        if self.krx_trading_suspended is True:
            return REASON_KRX_SUSPENDED
        if self.krx_trading_suspended is not False:
            return REASON_KRX_SUSPENDED_UNKNOWN
        return REASON_TRADABLE

    @property
    def krx_after_tradable(self) -> bool:
        return self.reason == REASON_TRADABLE

    def is_stale(self, *, now: dt.datetime | None = None) -> bool:
        if self.asof is None:
            return True
        current = now or dt.datetime.now(_KST)
        if current.tzinfo is None:
            current = current.replace(tzinfo=_KST)
        asof = (
            self.asof
            if self.asof.tzinfo is not None
            else self.asof.replace(tzinfo=_KST)
        )
        return (current - asof) > KRX_AFTER_LIST_STALE_AFTER

    def public_fields(self, *, now: dt.datetime | None = None) -> dict[str, Any]:
        stale = self.is_stale(now=now)
        if stale:
            reason = REASON_MISSING_ASOF if self.asof is None else REASON_STALE_ASOF
        else:
            reason = self.reason
        return {
            "krx_after_tradable": False if stale else self.krx_after_tradable,
            "krx_after_tradable_source": self.source,
            "krx_after_tradable_list_source": self.list_source,
            "krx_after_tradable_asof": (
                self.asof.isoformat() if self.asof is not None else None
            ),
            "krx_after_tradable_stale": stale,
            "krx_after_tradable_reason": reason,
        }


def krx_after_unknown_fields(reason: str) -> dict[str, Any]:
    """Public fields for a symbol whose capability could not be read at all."""
    return {
        "krx_after_tradable": False,
        "krx_after_tradable_source": KRX_AFTER_MARKET_SOURCE,
        "krx_after_tradable_list_source": None,
        "krx_after_tradable_asof": None,
        "krx_after_tradable_stale": True,
        "krx_after_tradable_reason": reason,
    }

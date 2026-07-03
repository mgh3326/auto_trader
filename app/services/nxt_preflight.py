from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from app.services.brokers.toss.market_calendar import KrTossSession

RETRY_AT_REGULAR = "retry_at_regular"
ROUTE_VIA_KIS = "route_via_kis"

_KST = dt.timezone(dt.timedelta(hours=9))
_NXT_SESSIONS: frozenset[str] = frozenset({"nxt_premarket", "nxt_after"})
# ROB-668: the toss_master_updated_at flag is refreshed by the operator sync
# (scripts/sync_kr_symbol_universe.py). Treat anything older than this as stale
# so the caller can decide whether to trust the eligibility bit.
NXT_FLAG_STALE_AFTER = dt.timedelta(days=2)


@dataclass(frozen=True)
class NxtTradability:
    nxt_eligible: bool
    nxt_trading_suspended: bool | None
    asof: dt.datetime | None
    source: str = "kr_symbol_universe"

    @property
    def nxt_tradable(self) -> bool:
        return self.nxt_eligible and self.nxt_trading_suspended is not True

    def is_stale(self, *, now: dt.datetime | None = None) -> bool:
        if self.asof is None:
            return True
        current = now or dt.datetime.now(_KST)
        asof = (
            self.asof
            if self.asof.tzinfo is not None
            else self.asof.replace(tzinfo=_KST)
        )
        return (current - asof) > NXT_FLAG_STALE_AFTER

    def public_fields(self, *, now: dt.datetime | None = None) -> dict[str, Any]:
        return {
            "nxt_tradable": self.nxt_tradable,
            "nxt_tradable_source": self.source,
            "nxt_tradable_asof": self.asof.isoformat()
            if self.asof is not None
            else None,
            "nxt_tradable_stale": self.is_stale(now=now),
        }


@dataclass(frozen=True)
class NxtPreflightVerdict:
    block: bool
    reason: str | None
    session: KrTossSession | None
    alternatives: tuple[str, ...]
    advisory: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "block": self.block,
            "reason": self.reason,
            "session": self.session,
            "alternatives": list(self.alternatives),
            "advisory": self.advisory,
        }


def evaluate_nxt_preflight(
    session: KrTossSession | None,
    tradability: NxtTradability,
) -> NxtPreflightVerdict:
    """Map (session) × (nxt_eligible, nxt_trading_suspended) -> verdict.

    Fail-open: session None (Toss calendar unavailable) -> advisory, never block.
    regular/closed -> ok (KRX path handles routing). Block only when the current
    session is an NXT window AND the symbol is not NXT-tradable.
    """
    if session is None:
        return NxtPreflightVerdict(
            block=False,
            reason="nxt_session_unavailable",
            session=None,
            alternatives=(),
            advisory=True,
        )
    if session not in _NXT_SESSIONS:
        return NxtPreflightVerdict(
            block=False, reason=None, session=session, alternatives=(), advisory=False
        )
    if tradability.nxt_tradable:
        return NxtPreflightVerdict(
            block=False, reason=None, session=session, alternatives=(), advisory=False
        )
    reason = (
        "nxt_trading_suspended"
        if tradability.nxt_trading_suspended is True
        else "not_nxt_eligible"
    )
    return NxtPreflightVerdict(
        block=True,
        reason=reason,
        session=session,
        alternatives=(RETRY_AT_REGULAR, ROUTE_VIA_KIS),
        advisory=False,
    )

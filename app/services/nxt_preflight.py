from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Literal

from app.services.brokers.toss.market_calendar import KrTossSession

RETRY_AT_REGULAR = "retry_at_regular"
ROUTE_VIA_KIS = "route_via_kis"

# #969: the KRX after-market (16:00-20:00 KST, #925) opens the after-hours
# session to a non-NXT name that is proven on the KRX list. These reasons let
# the desk tell "list missing / stale" apart from "not eligible".
KRX_AFTER_SESSION_LABEL = "krx_after"
REASON_KRX_AFTER_TRADABLE = "krx_after_tradable"
REASON_KRX_AFTER_CAPABILITY_UNKNOWN = "krx_after_capability_unknown"
REASON_KRX_AFTER_CAPABILITY_STALE = "krx_after_capability_stale"
# Resolver details (app.services.order_proposals.approval_window.
# resolve_krx_after_capability) that mean "no usable list", not "not eligible".
_KRX_AFTER_UNKNOWN_DETAILS: frozenset[str] = frozenset(
    {
        "krx_after_capability_lookup_failed",
        "krx_after_capability_unavailable",
        "krx_after_capability_list_missing",
    }
)
_KRX_AFTER_STALE_DETAILS: frozenset[str] = frozenset({"krx_after_capability_stale"})

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
        stale = self.is_stale(now=now)
        fields: dict[str, Any] = {
            "nxt_tradable": None if stale else self.nxt_tradable,
            "nxt_tradable_source": self.source,
            "nxt_tradable_asof": self.asof.isoformat()
            if self.asof is not None
            else None,
            "nxt_tradable_stale": stale,
        }
        if stale:
            fields["nxt_tradable_observed"] = self.nxt_tradable
            fields["nxt_tradable_reason"] = (
                "missing_asof" if self.asof is None else "stale_asof"
            )
        return fields


@dataclass(frozen=True)
class KrxAfterEvidence:
    """KRX after-market evidence for one symbol at one moment (#969).

    ``in_window`` is True only inside the Toss integrated after window clipped
    to 16:00-20:00 KST (``kr_krx_after_session_for``). ``allow`` and
    ``detail`` are the #925 capability resolver output, unchanged.
    """

    in_window: bool
    allow: bool
    detail: str


@dataclass(frozen=True)
class NxtPreflightVerdict:
    block: bool
    reason: str | None
    session: KrTossSession | Literal["krx_after"] | None
    alternatives: tuple[str, ...]
    advisory: bool
    krx_after_detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "block": self.block,
            "reason": self.reason,
            "session": self.session,
            "alternatives": list(self.alternatives),
            "advisory": self.advisory,
        }
        if self.krx_after_detail is not None:
            payload["krx_after_detail"] = self.krx_after_detail
        return payload


def needs_krx_after_evidence(
    session: KrTossSession | None, tradability: NxtTradability
) -> bool:
    """Only a non-NXT-tradable name in the after-hours session needs the list.

    NXT names never trigger the KRX lookup, and 08:00-08:50 stays NXT-only.
    """
    return session == "nxt_after" and not tradability.nxt_tradable


def evaluate_nxt_preflight(
    session: KrTossSession | None,
    tradability: NxtTradability,
    krx_after: KrxAfterEvidence | None = None,
) -> NxtPreflightVerdict:
    """Map (session) × (nxt_eligible, nxt_trading_suspended) -> verdict.

    Fail-open: session None (Toss calendar unavailable) -> advisory, never block.
    regular/closed -> ok (KRX path handles routing). Block only when the current
    session is an NXT window AND the symbol is not NXT-tradable.

    #969: inside the KRX after-market window (``krx_after.in_window``) a
    non-NXT name is allowed exactly when the #925 resolver allows it, the
    same rule the approval window applies. A missing, unreadable or stale list
    blocks with ``krx_after_capability_unknown`` / ``_stale``; a readable list
    that does not make the name eligible keeps the NXT reason. Without
    evidence (``krx_after`` None or outside the window) nothing changes.
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
    krx_after_detail: str | None = None
    if session == "nxt_after" and krx_after is not None and krx_after.in_window:
        krx_after_detail = krx_after.detail
        if krx_after.allow:
            return NxtPreflightVerdict(
                block=False,
                reason=REASON_KRX_AFTER_TRADABLE,
                session=KRX_AFTER_SESSION_LABEL,
                alternatives=(),
                advisory=False,
                krx_after_detail=krx_after_detail,
            )
        if krx_after.detail in _KRX_AFTER_STALE_DETAILS:
            reason = REASON_KRX_AFTER_CAPABILITY_STALE
        elif krx_after.detail in _KRX_AFTER_UNKNOWN_DETAILS:
            reason = REASON_KRX_AFTER_CAPABILITY_UNKNOWN
    return NxtPreflightVerdict(
        block=True,
        reason=reason,
        session=session,
        alternatives=(RETRY_AT_REGULAR, ROUTE_VIA_KIS),
        advisory=False,
        krx_after_detail=krx_after_detail,
    )

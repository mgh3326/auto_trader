"""Scope of each ``retro_funnel`` stage: which live brokers it read and on which date.

Every stage reports the same two facts the reader's statements already apply:

* ``brokers`` — the brokers whose live population the stage's query selects, read
  from the account-mode / ledger-scope literals in ``chain.py`` (the very constants
  the statements bind), narrowed to the brokers that carry this market.
* ``date_basis`` — the column the stage filters its KST window on.

Candidate and rejected-forecast populations are not account-scoped, so they carry
only ``date_basis``. The consistency between these values and the executed SQL is
pinned by ``tests/services/retro_v1/test_funnel_scope.py``.
"""

from __future__ import annotations

from typing import Any, Final

from app.services.retro_v1.chain import (
    KIS_ORDER_ACCOUNT_MODE,
    LIVE_ORDER_SCOPES,
    LIVE_PROPOSAL_ACCOUNT_MODES,
    TOSS_ORDER_ACCOUNT_MODE,
)

BROKER_ORDER: Final = ("kis", "toss", "upbit")

#: Brokers that trade each market in this codebase: KIS and Toss trade KR and US
#: (the Toss Open API contract is KR/US only), Upbit trades crypto.
MARKET_BROKERS: Final = {
    "kr": frozenset({"kis", "toss"}),
    "us": frozenset({"kis", "toss"}),
    "crypto": frozenset({"upbit"}),
}

#: Account-mode / ledger-scope literal -> broker.
_BROKER_BY_LITERAL: Final = {
    KIS_ORDER_ACCOUNT_MODE: "kis",
    TOSS_ORDER_ACCOUNT_MODE: "toss",
    "upbit": "upbit",
    "upbit_live": "upbit",
}

_PROPOSAL_BROKERS: Final = frozenset(
    _BROKER_BY_LITERAL[m] for m in LIVE_PROPOSAL_ACCOUNT_MODES
)
_ORDER_BROKERS: Final = frozenset(
    _BROKER_BY_LITERAL[m]
    for m in (KIS_ORDER_ACCOUNT_MODE, TOSS_ORDER_ACCOUNT_MODE, *LIVE_ORDER_SCOPES)
)
#: The fill query has no broker filter: every live broker's fills are read.
_FILL_BROKERS: Final = frozenset(BROKER_ORDER)

#: Stage -> (date_basis, brokers the stage reads; ``None`` = not account-scoped).
_STAGE_SCOPE: Final[dict[str, tuple[str, frozenset[str] | None]]] = {
    "candidate": ("candidate_recorded_at_kst", None),
    "proposal": ("proposal_created_kst", _PROPOSAL_BROKERS),
    "rung": ("proposal_created_kst", _PROPOSAL_BROKERS),
    "order": ("order_trade_date_kst", _ORDER_BROKERS),
    "fill": ("fill_filled_at_kst", _FILL_BROKERS),
    "rejected_forecast": ("forecast_created_kst", None),
}

#: date_basis -> (table, column) pairs the stage's KST window is applied to.
DATE_COLUMNS: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "candidate_recorded_at_kst": (("review.screener_pick_log", "recorded_at"),),
    "forecast_created_kst": (("review.trade_forecasts", "created_at"),),
    "proposal_created_kst": (("review.order_proposals", "created_at"),),
    "order_trade_date_kst": (
        ("review.kis_live_order_ledger", "trade_date"),
        ("review.live_order_ledger", "trade_date"),
        ("review.toss_live_order_ledger", "trade_date"),
    ),
    "fill_filled_at_kst": (("review.execution_ledger", "filled_at"),),
}


def stage_scope(stage: str, market: str) -> dict[str, Any]:
    """Scope fields for one stage row. Absent values are omitted, never null."""
    date_basis, brokers = _STAGE_SCOPE[stage]
    scope: dict[str, Any] = {"date_basis": date_basis}
    if brokers is not None:
        scope["account_mode"] = "live"
        scope["brokers"] = [
            broker
            for broker in BROKER_ORDER
            if broker in brokers and broker in MARKET_BROKERS[market]
        ]
    return scope

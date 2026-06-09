# app/services/brokers/kis/live_order_expiry.py
"""ROB-476 — pure day-order expiry classifier for KIS live KR orders.

Decides whether a still-unfilled (PENDING-verdict) day order should be resolved
to ``expired`` or kept ``pending``. stdlib-only: no broker / DB / network / clock
import, so it is unit-tested in isolation and the caller injects ``market_closed``
(computed from kr_market_data_state) and the broker rows.

Fail-closed + NXT-aware: a broker status token that says the order is still LIVE
(접수/정상/체결대기) keeps it ``pending`` regardless of the clock — an SOR order may
still be alive in the NXT session after KRX close. The time-guard only fires when
the status is non-informative AND the KRX session is closed.

The exact KIS status strings differ across surfaces and MUST be confirmed by a
read-only operator smoke (mirror of fill_evidence.py); the token lists below are
intention conservative.
"""

from __future__ import annotations

from typing import Any

_ORDER_NO_KEYS = ("odno", "ord_no")
_STATUS_KEYS = ("prcs_stat_name", "rvse_cncl_dvsn_name")
_CANCEL_DVSN_KEYS = ("rvse_cncl_dvsn_cd", "rvse_cncl_dvsn_name")

# Conservative tokens — confirm exact values via operator smoke before tightening.
_LIVE_TOKENS = ("접수", "정상", "체결대기", "유효")
_TERMINAL_TOKENS = ("취소", "거부", "거절", "실효", "만료")


def _lower_keys(row: dict[str, Any]) -> dict[str, Any]:
    return {str(k).lower(): v for k, v in row.items()}


def _first(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _matched_rows(rows: list[dict[str, Any]], order_no: str | None) -> list[dict]:
    if not order_no:
        return []
    out = []
    for raw in rows:
        row = _lower_keys(raw)
        if _first(row, _ORDER_NO_KEYS) == str(order_no):
            out.append(row)
    return out


def classify_day_order_expiry(
    *, rows: list[dict[str, Any]], order_no: str | None, market_closed: bool
) -> str:
    """Return ``"expired"`` or ``"pending"`` for a still-unfilled day order.

    - LIVE status token present → ``pending`` (may be alive in NXT; fail-closed).
    - TERMINAL status token present → ``expired``.
    - No informative token + KRX session closed → ``expired`` (time-guard).
    - Otherwise → ``pending``.
    """
    matched = _matched_rows(rows, order_no)
    if not matched:
        return "pending"  # not this branch's responsibility

    statuses = " ".join(_first(r, _STATUS_KEYS) for r in matched)
    cancel_dvsn = " ".join(_first(r, _CANCEL_DVSN_KEYS) for r in matched)
    blob = f"{statuses} {cancel_dvsn}"

    if any(tok in blob for tok in _LIVE_TOKENS):
        return "pending"
    if any(tok in blob for tok in _TERMINAL_TOKENS):
        return "expired"
    if market_closed:
        return "expired"
    return "pending"

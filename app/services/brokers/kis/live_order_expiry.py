# app/services/brokers/kis/live_order_expiry.py
"""ROB-476/ROB-487 — pure day-order expiry classifier for KIS live KR orders.

Decides whether a still-unfilled (PENDING-verdict) day order should be resolved
to ``expired`` / ``cancelled`` or kept ``pending``. stdlib-only: no broker / DB /
network / clock import — the caller injects ``nxt_closed`` (and ``now`` for
:func:`nxt_session_closed`) plus the broker rows, so the logic is unit-tested in
isolation and cannot fabricate a terminal status.

Live-verified TTTC8001R row shape (2026-06-10 read-only probe, windows
20260608/09/10): rows carry ``odno`` / ``orgn_odno`` / ``ord_qty`` /
``tot_ccld_qty`` / ``rjct_qty`` / ``rmn_qty`` / ``cncl_yn`` /
``sll_buy_dvsn_cd_name`` — and do NOT carry ``prcs_stat_name`` or
``rvse_cncl_dvsn_cd`` / ``rvse_cncl_dvsn_name`` (the previous ROB-476
status-token classifier could never engage on real data). Classification is
evidence-first on the real keys, fail-closed to ``pending``:

- cancel evidence → ``cancelled``: ``cncl_yn`` truthy on a matched row, or a
  cancel-confirm row (``orgn_odno`` matches the order, '취소' in
  ``sll_buy_dvsn_cd_name`` e.g. '매수취소'/'매도취소'). Valid at any time.
- broker expiry evidence → ``expired``: ``rjct_qty == ord_qty > 0`` (KIS
  expresses end-of-day day-order expiry as a full reject — live-verified on
  all 15 expired/cancelled 6/8-6/9 orders), gated on ``nxt_closed`` because
  whether ``rjct_qty`` is populated intraday is unconfirmed.
- otherwise → ``pending`` (incl. live evidence ``rmn_qty > 0``). A bare
  time-guard without broker evidence no longer expires anything.

Every rule is an any-row predicate (never a sum), so the live-observed
TTTC8001R pagination duplication (each row returned exactly twice) cannot
double-count.
"""

from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

_KST = datetime.timezone(datetime.timedelta(hours=9))

# NXT(대체거래소) 세션 마감 — SOR day order는 이 시각까지 살아있을 수 있다.
NXT_CLOSE_KST = datetime.time(hour=20, minute=0)

# --- ROB-671: offline accept-session × side day-order expiry ----------------
# stdlib-only KST wall-clock windows (no calendar): a same-day accept timestamp
# is classified into a trading window so expected_expiry/expiry_reason can vary
# by session × side without any broker/DB/network dependency in the send path.
_PREMARKET_OPEN = datetime.time(hour=8, minute=0)
_PREMARKET_CLOSE = datetime.time(hour=8, minute=50)
_REGULAR_OPEN = datetime.time(hour=9, minute=0)
_REGULAR_CLOSE = datetime.time(hour=15, minute=30)
_NXT_AFTER_OPEN = datetime.time(hour=16, minute=0)
# _NXT_AFTER_CLOSE == NXT_CLOSE_KST (20:00)

SESSION_PREMARKET = "premarket"
SESSION_REGULAR = "regular"
SESSION_NXT_AFTER = "nxt_after"
SESSION_OFF = "off"

# Categorical expiry_reason vocabulary (never a fabricated timestamp/fill).
REASON_NXT_CARRY = (
    "nxt_carry"  # premarket/nxt_after/regular-sell → 20:00 (SOR NXT carry)
)
REASON_REGULAR_BUY_CONSERVATIVE = "regular_buy_conservative_20_00"
REASON_REGULAR_BUY_UNSETTLED_1530 = "regular_buy_unsettled_15_30"  # gated downgrade
REASON_UNKNOWN_SESSION = "unknown_session"  # off-window accept → conservative 20:00


_ORDER_NO_KEYS = ("odno", "ord_no")
_ORIGIN_ORDER_NO_KEYS = ("orgn_odno", "orgn_ord_no")
_SIDE_NAME_KEYS = ("sll_buy_dvsn_cd_name", "sll_buy_dvsn_name")
_CANCEL_FLAG_KEYS = ("cncl_yn",)
# US(해외) 취소 증거: 정정취소구분명이 '취소'를 포함한다 (TTTS3018R/일별체결).
_RVSE_CANCEL_NAME_KEYS = ("rvse_cncl_dvsn_name",)
_ORD_QTY_KEYS = ("ord_qty",)
_RJCT_QTY_KEYS = ("rjct_qty",)

_CANCEL_TOKEN = "취소"
_TRUTHY_FLAGS = frozenset({"y", "yes", "true", "1"})


def nxt_session_closed(*, order_date: datetime.date, now: datetime.datetime) -> bool:
    """True iff ``now`` is at/after the NXT close (20:00 KST) of ``order_date``.

    Naive ``now`` is assumed KST (app/core/timezone convention). Pure function:
    the caller injects ``now`` — no clock import here.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=_KST)
    close = datetime.datetime.combine(order_date, NXT_CLOSE_KST, tzinfo=_KST)
    return now.astimezone(_KST) >= close


def classify_kr_accept_session(accepted_at: datetime.datetime) -> str:
    """Classify a KST accept timestamp into a KR trading window (stdlib-only).

    Windows (KST, close exclusive): premarket 08:00–08:50, regular 09:00–15:30,
    nxt_after 16:00–20:00; anything else (incl. the 15:30–16:00 gap) → ``off``.
    Naive timestamps are assumed KST (app/core/timezone convention).
    """
    if accepted_at.tzinfo is None:
        accepted_at = accepted_at.replace(tzinfo=_KST)
    t = accepted_at.astimezone(_KST).time()
    if _PREMARKET_OPEN <= t < _PREMARKET_CLOSE:
        return SESSION_PREMARKET
    if _REGULAR_OPEN <= t < _REGULAR_CLOSE:
        return SESSION_REGULAR
    if _NXT_AFTER_OPEN <= t < NXT_CLOSE_KST:
        return SESSION_NXT_AFTER
    return SESSION_OFF


def kr_day_order_expiry(
    *,
    accepted_at: datetime.datetime,
    side: str,
    accept_session: str | None = None,
    unsettled_regular_buy_downgrade: bool = False,
) -> tuple[str | None, str]:
    """Return ``(expiry_iso, expiry_reason)`` for a KR day order by session × side.

    Conservative default (ROB-671): a regular-session BUY resolves to 20:00 KST
    (today's behavior) with ``REASON_REGULAR_BUY_CONSERVATIVE`` — the 15:30 death
    observed on regular-session buys may be a D+2 unsettled-cash cancel rather
    than pure session expiry, so it is NOT applied by default. Set
    ``unsettled_regular_buy_downgrade=True`` (operator flag) only once a live
    measurement confirms the cause. Regular-session SELLs, premarket, and
    nxt_after all carry to the NXT close (20:00). Returns ``(None, reason)`` only
    if the timestamp cannot be localized.
    """
    if accepted_at.tzinfo is None:
        local = accepted_at.replace(tzinfo=_KST)
    else:
        local = accepted_at.astimezone(_KST)
    session = accept_session or classify_kr_accept_session(local)
    normalized_side = (side or "").strip().lower()

    def _iso(t: datetime.time) -> str:
        return local.replace(
            hour=t.hour, minute=t.minute, second=0, microsecond=0
        ).isoformat()

    if session == SESSION_REGULAR and normalized_side == "buy":
        if unsettled_regular_buy_downgrade:
            return _iso(_REGULAR_CLOSE), REASON_REGULAR_BUY_UNSETTLED_1530
        return _iso(NXT_CLOSE_KST), REASON_REGULAR_BUY_CONSERVATIVE
    if session == SESSION_OFF:
        # Accepted outside any known window → keep 20:00 but flag the uncertainty.
        return _iso(NXT_CLOSE_KST), REASON_UNKNOWN_SESSION
    # premarket / nxt_after / regular-sell → confident NXT carry to 20:00.
    return _iso(NXT_CLOSE_KST), REASON_NXT_CARRY


def parse_kis_ordered_at(ordered_at: str | None) -> datetime.datetime | None:
    """Parse a KIS ``'YYYYMMDD HHMMSS'`` (KST) string to a tz-aware datetime.

    Tolerates a short HHMM time (right-padded to HHMMSS). Returns None on any
    malformed input — the caller then omits the derived reason.
    """
    if not ordered_at:
        return None
    parts = ordered_at.strip().split()
    if len(parts) < 2:
        return None
    ord_dt, ord_tmd = parts[0], parts[1]
    if len(ord_dt) < 8 or not ord_dt[:8].isdigit():
        return None
    tmd = "".join(ch for ch in ord_tmd if ch.isdigit())
    if len(tmd) < 4:
        return None
    tmd = (tmd + "000000")[:6]
    try:
        return datetime.datetime.strptime(ord_dt[:8] + tmd, "%Y%m%d%H%M%S").replace(
            tzinfo=_KST
        )
    except ValueError:
        return None


def _lower_keys(row: dict[str, Any]) -> dict[str, Any]:
    return {str(k).lower(): v for k, v in row.items()}


def _first(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _to_decimal(value: str) -> Decimal | None:
    text = value.replace(",", "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _order_no_matches(target: str, candidate: str) -> bool:
    # fill_evidence._order_no_matches 와 동일한 leading-zero 정규화 (구 분류기의
    # exact-match 불일치 해소 — ROB-487).
    if not candidate:
        return False
    return candidate == target or candidate.lstrip("0") == target.lstrip("0")


def _is_truthy_flag(value: str) -> bool:
    return value.strip().lower() in _TRUTHY_FLAGS


def row_has_cancel_evidence(row: dict[str, Any]) -> bool:
    """True iff a *single* broker row carries direct cancel evidence.

    ROB-665: the read-path order-history normalizers process one order at a
    time, so they cannot run the cross-row ``orgn_odno`` match that
    :func:`classify_day_order_expiry` does. This per-row predicate reuses the
    same real-field signals that exist on the order's own row:

    - ``cncl_yn`` truthy (KR TTTC8001R — ``prcs_stat_name`` does not exist), or
    - a '취소' token in the side name (a cancel-confirm row is '매수취소' /
      '매도취소'), or
    - a '취소' token in ``rvse_cncl_dvsn_name`` (US 정정취소구분명).

    Evidence-first: this must win over the EOD death rule so an operator cancel
    is labelled ``cancelled``, not ``expired``.
    """
    r = _lower_keys(row)
    if _is_truthy_flag(_first(r, _CANCEL_FLAG_KEYS)):
        return True
    if _CANCEL_TOKEN in _first(r, _SIDE_NAME_KEYS):
        return True
    if _CANCEL_TOKEN in _first(r, _RVSE_CANCEL_NAME_KEYS):
        return True
    return False


def classify_day_order_expiry(
    *, rows: list[dict[str, Any]], order_no: str | None, nxt_closed: bool
) -> str:
    """Return ``"cancelled"`` / ``"expired"`` / ``"pending"`` for an unfilled day order.

    Fail-closed: without broker cancel/expiry evidence the order stays
    ``pending`` — even after NXT close. Evidence-first booking makes a late
    terminal marking harmless; a premature one (the 6/9 19:02 run expired SOR
    orders 58 minutes before NXT close) is the failure mode this prevents.
    """
    target = str(order_no or "").strip()
    if not target:
        return "pending"

    direct: list[dict[str, Any]] = []
    cancel_confirms: list[dict[str, Any]] = []
    for raw in rows:
        row = _lower_keys(raw)
        if _order_no_matches(target, _first(row, _ORDER_NO_KEYS)):
            direct.append(row)
        elif _order_no_matches(target, _first(row, _ORIGIN_ORDER_NO_KEYS)):
            cancel_confirms.append(row)

    if not direct and not cancel_confirms:
        return "pending"  # not this branch's responsibility (NONE-verdict path)

    # 1) Cancel evidence — broker-confirmed, valid at any time of day.
    if any(_is_truthy_flag(_first(r, _CANCEL_FLAG_KEYS)) for r in direct):
        return "cancelled"
    if any(_CANCEL_TOKEN in _first(r, _SIDE_NAME_KEYS) for r in cancel_confirms):
        return "cancelled"

    # 2) Broker end-of-day expiry evidence, gated on NXT close (20:00 KST).
    if nxt_closed:
        for r in direct:
            ord_qty = _to_decimal(_first(r, _ORD_QTY_KEYS))
            rjct_qty = _to_decimal(_first(r, _RJCT_QTY_KEYS))
            if (
                ord_qty is not None
                and rjct_qty is not None
                and ord_qty > 0
                and rjct_qty == ord_qty
            ):
                return "expired"

    # 3) Fail-closed: live (rmn_qty > 0) or non-informative → pending.
    return "pending"

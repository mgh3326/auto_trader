# KIS expected_expiry by accept-session × side Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax.

**Goal:** Make `kis_live_place_order`'s `expected_expiry` a function of the KR accept-session × order side (plus a categorical `expiry_reason`), emit a death-risk-aware dynamic `routing.note`, and surface `expiry_reason` on `kis_live_get_order_history` rows — shipping with a conservative default (regular-session buy still → 20:00, matching today) while implementing but gating-off the aggressive "unsettled regular buy → 15:30" downgrade behind a settings flag pending a live measurement.

**Architecture:** One stdlib-only offline classifier (no broker/DB/network/calendar — keeps the order-send hot path free of `TOSS_API_ENABLED`/network) lives in `app/services/brokers/kis/live_order_expiry.py` and is the single source of truth for both the send-path computer (`kis_live_ledger.py`) and the snapshot collector computer (`pending_orders.py`). The classifier maps a KST accept timestamp to a session window (premarket / regular / nxt_after / off) and returns `(expiry_iso, expiry_reason)` given the side; the 15:30 downgrade is a bool arg wired to a default-off settings flag. Read-path order-history normalizers derive the categorical `expiry_reason` from the row's own `ordered_at` + side.

**Tech Stack:** Python 3.13, stdlib `datetime` only in the classifier, pydantic-settings (`app.core.config.Settings`), pytest (markers: unit).

## Global Constraints

- **Migration-0.** No new DB columns, enums, or alembic revisions. `expected_expiry`/`expiry_reason` are response-only fields; the settings flag is env-only. Do NOT run `alembic revision`.
- **Offline / network-free classifier.** `app/services/brokers/kis/live_order_expiry.py` must stay stdlib-only: no broker client, no DB session, no `exchange_calendars`/`pandas`, no `TOSS_API_ENABLED`, no clock import (caller injects the timestamp). This is the reason the classifier does NOT reuse `app/mcp_server/tooling/market_session.py` (imports `exchange_calendars`+`pandas`) nor `app/services/brokers/toss/market_calendar.py` (Toss-API-backed / network).
- **Disjoint from ROB-668.** Do NOT edit `app/services/brokers/toss/market_calendar.py` or any Toss order tooling — that is ROB-668's territory.
- **Conservative default.** The default (`kis_regular_buy_unsettled_expiry_1530=False`) MUST keep regular-session-buy `expected_expiry` at 20:00 KST (today's behavior). The 15:30 downgrade branch is implemented and unit-tested but never on by default.
- **Evidence honesty.** `expiry_reason` is categorical (a session×side classification), never a fabricated fill/terminal claim. The reconcile terminal classifier (`classify_day_order_expiry`) is unchanged and remains evidence-first / fail-closed.
- **Test runner:** `uv run pytest <path> -v`. Lint: `make lint`.

---

## File Structure

| File | Create/Modify | Responsibility |
|------|---------------|----------------|
| `app/services/brokers/kis/live_order_expiry.py` | Modify | Add offline accept-session classifier (`classify_kr_accept_session`), the shared `(expiry_iso, expiry_reason)` computer (`kr_day_order_expiry`), the ordered_at parser (`parse_kis_ordered_at`), and session/reason constants. Existing reconcile classifier untouched. |
| `app/core/config.py` | Modify | Add `kis_regular_buy_unsettled_expiry_1530: bool = False` gate. |
| `app/mcp_server/tooling/kis_live_ledger.py` | Modify | Rewrite `_expected_day_order_expiry` to `(now, side, …) -> (iso, reason)`; add `_build_kr_routing_note`; wire dynamic `routing.note` + `expected_expiry` + `expiry_reason` into `_record_kis_live_order`. |
| `app/mcp_server/tooling/orders_modify_cancel.py` | Modify | Emit categorical `expiry_reason` on `_normalize_kis_domestic_order` (KR classifier) and `_normalize_kis_overseas_order` (US placeholder constant). |
| `app/services/action_report/snapshot_backed/collectors/pending_orders.py` | Modify | Refactor `_kis_expected_expiry` to delegate to the shared helper (threading `side`); add `expiry_reason` to the KR normalized payload (additive). |
| `tests/services/brokers/kis/test_live_order_expiry_session.py` | Create | Unit tests for the classifier + computer + parser. |
| `tests/mcp_server/test_kis_live_place_order_routing_surface.py` | Modify | Update the pinned-20:00 test to the new signature; add reason/routing-note assertions. |
| `tests/mcp_server/tooling/test_orders_history_expiry_reason.py` | Create | Unit tests for history `expiry_reason` (KR + US). |
| `tests/services/action_report/snapshot_backed/test_pending_orders_collector.py` | Modify | Add `expiry_reason` assertion; keep the 20:00 default assertions green. |
| `CLAUDE.md` | Modify | Document accept-session×side expiry, regular-sell NXT-carry, and the open buy-death-cause question. |
| `docs/runbooks/kis-live-order-reconcile.md` | Modify | Add the session×side expiry semantics + the gated 15:30 flag + open measurement. |

---

## Task 1 — Offline accept-session classifier + shared expiry/reason computer

**Files:**
- Modify: `app/services/brokers/kis/live_order_expiry.py` (add after `NXT_CLOSE_KST` at line 42 and after `nxt_session_closed` at line 66; do NOT touch `classify_day_order_expiry`/`row_has_cancel_evidence`)
- Test: `tests/services/brokers/kis/test_live_order_expiry_session.py` (create)

**Interfaces:**
- Produces `classify_kr_accept_session(accepted_at: datetime.datetime) -> str` ∈ {`"premarket"`,`"regular"`,`"nxt_after"`,`"off"`}
- Produces `kr_day_order_expiry(*, accepted_at: datetime.datetime, side: str, accept_session: str | None = None, unsettled_regular_buy_downgrade: bool = False) -> tuple[str | None, str]`
- Produces `parse_kis_ordered_at(ordered_at: str | None) -> datetime.datetime | None`
- Produces constants `SESSION_PREMARKET`, `SESSION_REGULAR`, `SESSION_NXT_AFTER`, `SESSION_OFF`, `REASON_NXT_CARRY`, `REASON_REGULAR_BUY_CONSERVATIVE`, `REASON_REGULAR_BUY_UNSETTLED_1530`, `REASON_UNKNOWN_SESSION`
- Consumes: stdlib `datetime` only.

TDD steps:

- [ ] Write failing test file `tests/services/brokers/kis/test_live_order_expiry_session.py`:
```python
import datetime

import pytest

from app.services.brokers.kis.live_order_expiry import (
    REASON_NXT_CARRY,
    REASON_REGULAR_BUY_CONSERVATIVE,
    REASON_REGULAR_BUY_UNSETTLED_1530,
    REASON_UNKNOWN_SESSION,
    SESSION_NXT_AFTER,
    SESSION_OFF,
    SESSION_PREMARKET,
    SESSION_REGULAR,
    classify_kr_accept_session,
    kr_day_order_expiry,
    parse_kis_ordered_at,
)

KST = datetime.timezone(datetime.timedelta(hours=9))


def _at(h, m):
    return datetime.datetime(2026, 7, 3, h, m, 0, tzinfo=KST)


@pytest.mark.unit
@pytest.mark.parametrize(
    "hh,mm,expected",
    [
        (8, 0, SESSION_PREMARKET),
        (8, 49, SESSION_PREMARKET),
        (8, 50, SESSION_OFF),  # premarket close is exclusive
        (9, 0, SESSION_REGULAR),
        (15, 29, SESSION_REGULAR),
        (15, 30, SESSION_OFF),  # regular close is exclusive
        (15, 45, SESSION_OFF),  # KRX-close↔NXT-open gap
        (16, 0, SESSION_NXT_AFTER),
        (19, 59, SESSION_NXT_AFTER),
        (20, 0, SESSION_OFF),  # NXT close is exclusive
        (7, 0, SESSION_OFF),
    ],
)
def test_classify_kr_accept_session_windows(hh, mm, expected):
    assert classify_kr_accept_session(_at(hh, mm)) == expected


@pytest.mark.unit
def test_classify_treats_naive_as_kst():
    naive = datetime.datetime(2026, 7, 3, 9, 30, 0)
    assert classify_kr_accept_session(naive) == SESSION_REGULAR


@pytest.mark.unit
def test_regular_sell_carries_to_nxt_2000():
    iso, reason = kr_day_order_expiry(accepted_at=_at(9, 30), side="sell")
    assert iso == "2026-07-03T20:00:00+09:00"
    assert reason == REASON_NXT_CARRY


@pytest.mark.unit
def test_regular_buy_conservative_default_is_2000():
    iso, reason = kr_day_order_expiry(accepted_at=_at(9, 30), side="buy")
    assert iso == "2026-07-03T20:00:00+09:00"
    assert reason == REASON_REGULAR_BUY_CONSERVATIVE


@pytest.mark.unit
def test_regular_buy_downgrade_gated_to_1530():
    iso, reason = kr_day_order_expiry(
        accepted_at=_at(9, 30), side="buy", unsettled_regular_buy_downgrade=True
    )
    assert iso == "2026-07-03T15:30:00+09:00"
    assert reason == REASON_REGULAR_BUY_UNSETTLED_1530


@pytest.mark.unit
def test_downgrade_does_not_touch_sell():
    iso, reason = kr_day_order_expiry(
        accepted_at=_at(9, 30), side="sell", unsettled_regular_buy_downgrade=True
    )
    assert iso == "2026-07-03T20:00:00+09:00"
    assert reason == REASON_NXT_CARRY


@pytest.mark.unit
@pytest.mark.parametrize("hh,mm", [(8, 10), (16, 30)])
def test_premarket_and_nxt_after_buy_carry_to_2000(hh, mm):
    iso, reason = kr_day_order_expiry(accepted_at=_at(hh, mm), side="buy")
    assert iso == "2026-07-03T20:00:00+09:00"
    assert reason == REASON_NXT_CARRY


@pytest.mark.unit
def test_off_session_is_unknown_reason_but_2000():
    iso, reason = kr_day_order_expiry(accepted_at=_at(15, 45), side="buy")
    assert iso == "2026-07-03T20:00:00+09:00"
    assert reason == REASON_UNKNOWN_SESSION


@pytest.mark.unit
def test_accept_session_override_skips_reclassify():
    # Pass a mismatching pre-classified session to prove it is honored verbatim.
    iso, reason = kr_day_order_expiry(
        accepted_at=_at(15, 45), side="buy", accept_session=SESSION_REGULAR
    )
    assert reason == REASON_REGULAR_BUY_CONSERVATIVE
    assert iso == "2026-07-03T20:00:00+09:00"


@pytest.mark.unit
def test_parse_kis_ordered_at_hhmmss():
    dt = parse_kis_ordered_at("20260703 093015")
    assert dt == datetime.datetime(2026, 7, 3, 9, 30, 15, tzinfo=KST)


@pytest.mark.unit
def test_parse_kis_ordered_at_short_time_padded():
    dt = parse_kis_ordered_at("20260703 0925")
    assert dt == datetime.datetime(2026, 7, 3, 9, 25, 0, tzinfo=KST)


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["", "  ", None, "20260703", "notadate 093015", "2026 09"])
def test_parse_kis_ordered_at_bad_returns_none(bad):
    assert parse_kis_ordered_at(bad) is None
```
- [ ] Run it, expect ImportError/collection failure: `uv run pytest tests/services/brokers/kis/test_live_order_expiry_session.py -v` → expected: `ImportError: cannot import name 'classify_kr_accept_session'`.
- [ ] Minimal impl — insert into `app/services/brokers/kis/live_order_expiry.py` immediately after `NXT_CLOSE_KST = datetime.time(hour=20, minute=0)` (line 42):
```python
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
REASON_NXT_CARRY = "nxt_carry"  # premarket/nxt_after/regular-sell → 20:00 (SOR NXT carry)
REASON_REGULAR_BUY_CONSERVATIVE = "regular_buy_conservative_20_00"
REASON_REGULAR_BUY_UNSETTLED_1530 = "regular_buy_unsettled_15_30"  # gated downgrade
REASON_UNKNOWN_SESSION = "unknown_session"  # off-window accept → conservative 20:00
```
- [ ] Minimal impl — insert after `nxt_session_closed` (after line 66):
```python
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
```
- [ ] Run it, expect pass: `uv run pytest tests/services/brokers/kis/test_live_order_expiry_session.py -v` → expected: all tests pass (green).
- [ ] Commit: `git add app/services/brokers/kis/live_order_expiry.py tests/services/brokers/kis/test_live_order_expiry_session.py && git commit -m "ROB-671: offline KR accept-session × side day-order expiry classifier"`

---

## Task 2 — Settings gate for the 15:30 downgrade (default off)

**Files:**
- Modify: `app/core/config.py` (KIS block, after `kis_mock_scalping_enabled: bool = False` at line 217)
- Test: covered by Task 3's integration assertions (no dedicated test — a single bool field with a default; TDD lands in Task 3 where it is consumed).

**Interfaces:**
- Produces `settings.kis_regular_buy_unsettled_expiry_1530: bool` (default `False`)

TDD steps:

- [ ] Minimal impl — insert into `app/core/config.py` after line 217 (`kis_mock_scalping_enabled: bool = False`):
```python
    # ROB-671: gate the aggressive "unsettled regular-session buy → 15:30 death"
    # expiry downgrade. Default off — a regular-session BUY keeps expected_expiry
    # at 20:00 KST (conservative). Flip to true ONLY after a live measurement
    # confirms the 15:30 death is session expiry (not a D+2 unsettled-cash cancel).
    kis_regular_buy_unsettled_expiry_1530: bool = False
```
- [ ] Run to prove the field loads: `uv run python -c "from app.core.config import settings; print(settings.kis_regular_buy_unsettled_expiry_1530)"` → expected: `False`
- [ ] Commit: `git add app/core/config.py && git commit -m "ROB-671: add default-off kis_regular_buy_unsettled_expiry_1530 gate"`

---

## Task 3 — Rewrite `_expected_day_order_expiry`, dynamic `routing.note`, `expiry_reason` in send response

**Files:**
- Modify: `app/mcp_server/tooling/kis_live_ledger.py` (imports line 31-34; rewrite `_expected_day_order_expiry` lines 170-182; `_record_kis_live_order` body lines 216-296)
- Test: `tests/mcp_server/test_kis_live_place_order_routing_surface.py` (modify)

**Interfaces:**
- Consumes `kr_day_order_expiry`, `classify_kr_accept_session`, `SESSION_REGULAR` from `live_order_expiry`; `settings` from `app.core.config`.
- Produces rewritten `_expected_day_order_expiry(now: datetime.datetime, *, side: str, accept_session: str | None = None, unsettled_regular_buy_downgrade: bool = False) -> tuple[str | None, str]`
- Produces `_build_kr_routing_note(*, side: str, accept_session: str) -> str`
- Produces `_record_kis_live_order` response with `expected_expiry` (iso), new `expiry_reason` (categorical), and dynamic `routing.note`.

TDD steps:

- [ ] Rewrite the test `tests/mcp_server/test_kis_live_place_order_routing_surface.py` — replace the whole file body with:
```python
# tests/mcp_server/test_kis_live_place_order_routing_surface.py
import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server.tooling import kis_live_ledger as mod
from app.mcp_server.tooling.kis_live_ledger import (
    _build_kr_routing_note,
    _expected_day_order_expiry,
    _extract_broker_exchange,
)
from app.services.brokers.kis.live_order_expiry import (
    REASON_NXT_CARRY,
    REASON_REGULAR_BUY_CONSERVATIVE,
    SESSION_REGULAR,
)

KST = datetime.timezone(datetime.timedelta(hours=9))


def test_expected_day_order_expiry_regular_buy_conservative_2000():
    # ROB-671: regular-session BUY keeps 20:00 by conservative default, but the
    # reason flags the 15:30 death uncertainty.
    now = datetime.datetime(2026, 6, 9, 9, 43, 25, tzinfo=KST)
    iso, reason = _expected_day_order_expiry(now, side="buy")
    assert iso == "2026-06-09T20:00:00+09:00"
    assert reason == REASON_REGULAR_BUY_CONSERVATIVE


def test_expected_day_order_expiry_regular_sell_nxt_carry():
    now = datetime.datetime(2026, 6, 9, 9, 43, 25, tzinfo=KST)
    iso, reason = _expected_day_order_expiry(now, side="sell")
    assert iso == "2026-06-09T20:00:00+09:00"
    assert reason == REASON_NXT_CARRY


def test_routing_note_regular_buy_warns_death_risk():
    note = _build_kr_routing_note(side="buy", accept_session=SESSION_REGULAR)
    assert "15:30" in note
    assert "remaining_qty" in note


def test_routing_note_sell_mentions_nxt_carry():
    note = _build_kr_routing_note(side="sell", accept_session=SESSION_REGULAR)
    assert "NXT" in note
    assert "20:00" in note


def test_extract_broker_exchange_present():
    raw = {"output": {"EXCG_ID_DVSN_CD": "KRX"}}
    assert _extract_broker_exchange(raw) == "KRX"


def test_extract_broker_exchange_absent_is_none():
    assert _extract_broker_exchange({"output": {}}) is None
    assert _extract_broker_exchange({}) is None


@pytest.mark.asyncio
async def test_place_order_response_surfaces_routing_and_reason():
    execution_result = {
        "odno": "0011001100",
        "ord_tmd": "094300",
        "rt_cd": "0",
        "msg1": "정상",
        "output": {"EXCG_ID_DVSN_CD": "KRX"},
    }
    dry_run_result = {"price": 209000, "quantity": 2, "estimated_value": 418000}
    fixed_now = datetime.datetime(2026, 6, 9, 9, 43, 0, tzinfo=KST)
    with (
        patch.object(mod, "_save_kis_live_order_ledger", AsyncMock(return_value=42)),
        patch.object(mod, "now_kst", lambda: fixed_now),
    ):
        resp = await mod._record_kis_live_order(
            normalized_symbol="005930",
            market_type="equity_kr",
            side="buy",
            order_type="limit",
            dry_run_result=dry_run_result,
            execution_result=execution_result,
            reason=None,
            exit_reason=None,
            thesis="t",
            strategy="s",
            target_price=None,
            stop_loss=None,
            min_hold_days=None,
            notes=None,
            indicators_snapshot=None,
        )
    assert resp["order_validity"] == "day"
    assert resp["routing"]["requested_venue"] == "auto"
    assert resp["broker_exchange"] == "KRX"
    assert resp["expected_expiry"] == "2026-06-09T20:00:00+09:00"
    assert resp["expiry_reason"] == REASON_REGULAR_BUY_CONSERVATIVE
    assert "15:30" in resp["routing"]["note"]
```
- [ ] Run it, expect failure: `uv run pytest tests/mcp_server/test_kis_live_place_order_routing_surface.py -v` → expected: `ImportError: cannot import name '_build_kr_routing_note'` (and signature failures).
- [ ] Minimal impl — extend the import in `app/mcp_server/tooling/kis_live_ledger.py` (lines 31-34) to:
```python
from app.services.brokers.kis.live_order_expiry import (
    SESSION_REGULAR,
    classify_day_order_expiry,
    classify_kr_accept_session,
    kr_day_order_expiry,
    nxt_session_closed,
)
```
- [ ] Minimal impl — add the settings import near the top imports of `kis_live_ledger.py` (after line 20 `from app.core.db import AsyncSessionLocal`):
```python
from app.core.config import settings
```
- [ ] Minimal impl — replace `_expected_day_order_expiry` (lines 170-182) with:
```python
def _expected_day_order_expiry(
    now: datetime.datetime,
    *,
    side: str,
    accept_session: str | None = None,
    unsettled_regular_buy_downgrade: bool = False,
) -> tuple[str | None, str]:
    """(ISO expiry, categorical reason) for a KR day order by accept-session × side.

    ROB-671: delegates to the stdlib-only classifier. Regular-session BUY stays
    20:00 KST by conservative default (the historical 15:30 death may be a D+2
    unsettled-cash cancel, not session expiry); the downgrade is gated by
    ``settings.kis_regular_buy_unsettled_expiry_1530``. Regular SELL / premarket /
    nxt_after carry to the NXT close (20:00).
    """
    try:
        return kr_day_order_expiry(
            accepted_at=now.astimezone(KST),
            side=side,
            accept_session=accept_session,
            unsettled_regular_buy_downgrade=unsettled_regular_buy_downgrade,
        )
    except (ValueError, OverflowError):
        return None, "unknown_session"
```
- [ ] Minimal impl — add `_build_kr_routing_note` immediately below `_expected_day_order_expiry`:
```python
def _build_kr_routing_note(*, side: str, accept_session: str) -> str:
    """Dynamic SOR routing note that warns about session × side death risk."""
    base = "SOR auto-route (KRX; NXT-eligible)"
    normalized_side = (side or "").strip().lower()
    if accept_session == SESSION_REGULAR and normalized_side == "buy":
        return (
            f"{base}. 정규장 매수: 미수/미결제(현금 미결제) 자금이면 15:30 소멸 위험 — "
            "체결/생존 여부는 remaining_qty(잔량)로 확인하세요."
        )
    if normalized_side == "sell":
        return f"{base}. NXT carry: SOR 현금매도는 NXT 마감(20:00 KST)까지 유효합니다."
    return f"{base}. NXT-eligible: 20:00 KST까지 유효합니다."
```
- [ ] Minimal impl — in `_record_kis_live_order`, compute the accept-session + expiry once. Insert right after the docstring / before `price_val = ...` (line 218):
```python
    now = now_kst()
    accept_session = classify_kr_accept_session(now)
    expiry_iso, expiry_reason = _expected_day_order_expiry(
        now,
        side=side,
        accept_session=accept_session,
        unsettled_regular_buy_downgrade=settings.kis_regular_buy_unsettled_expiry_1530,
    )
```
- [ ] Minimal impl — replace the `routing` + `expected_expiry` block in the returned dict (lines 284-288) with:
```python
        "routing": {
            "requested_venue": "auto",
            "note": _build_kr_routing_note(side=side, accept_session=accept_session),
        },
        "expected_expiry": expiry_iso,
        "expiry_reason": expiry_reason,
```
- [ ] Run it, expect pass: `uv run pytest tests/mcp_server/test_kis_live_place_order_routing_surface.py -v` → expected: all pass.
- [ ] Run the ledger regression suite to prove no break: `uv run pytest tests/mcp_server/tooling/test_kis_live_ledger.py tests/test_rob653_ledger_passthrough.py tests/test_rob473_report_item_link_threading.py -v` → expected: all pass (these do not assert `expected_expiry`/`routing.note`).
- [ ] Commit: `git add app/mcp_server/tooling/kis_live_ledger.py tests/mcp_server/test_kis_live_place_order_routing_surface.py && git commit -m "ROB-671: session×side expected_expiry + expiry_reason + dynamic routing.note in send response"`

---

## Task 4 — Refactor the collector's second computer to the shared helper

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/pending_orders.py` (`_kis_expected_expiry` lines 249-254; `_normalize_kis_order` lines 257-292)
- Test: `tests/services/action_report/snapshot_backed/test_pending_orders_collector.py` (modify)

**Interfaces:**
- Consumes `kr_day_order_expiry` from `live_order_expiry`.
- Produces refactored `_kis_expected_expiry(placed_at, *, market, side) -> tuple[str | None, str | None]` (iso, reason) and adds `expiry_reason` to the KR normalized dict (additive; US/crypto stay `None`).
  - **Annotation note:** the wrapper's return is `tuple[str | None, str | None]` (not the shared computer's `tuple[str | None, str]`) deliberately — the collector's `market != "kr" or placed_at is None` early return yields `(None, None)`, so the reason is genuinely `str | None` at this call site. Only the KR-with-timestamp branch delegates to `kr_day_order_expiry`, which always returns a non-None categorical `str`.

TDD steps:

- [ ] Update the existing test `test_normalize_kis_kr_order_adds_expected_day_expiry_from_placed_at` (line 274-293) to also assert the reason — append after line 293:
```python
    assert out["expiry_reason"] == "regular_buy_conservative_20_00"
```
- [ ] Add a new test after `test_normalize_kis_us_order_keeps_expected_expiry_unknown` (after line 314):
```python
def test_normalize_kis_kr_sell_order_reason_is_nxt_carry() -> None:
    from app.services.action_report.snapshot_backed.collectors.pending_orders import (
        _normalize_kis_order,
    )

    row = {
        "ord_no": "0011001101",
        "pdno": "005930",
        "sll_buy_dvsn_cd": "01",  # sell
        "ord_unpr": "70000",
        "ord_qty": "3",
        "nccs_qty": "3",
        "ord_dt": "20260611",
        "ord_tmd": "093000",
    }

    out = _normalize_kis_order(row, market="kr")

    assert out["expected_expiry"] == "2026-06-11T20:00:00+09:00"
    assert out["expiry_reason"] == "nxt_carry"


def test_normalize_kis_us_order_reason_is_none() -> None:
    from app.services.action_report.snapshot_backed.collectors.pending_orders import (
        _normalize_kis_order,
    )

    row = {
        "odno": "US-2",
        "pdno": "AAPL",
        "sll_buy_dvsn_cd": "02",
        "ord_unpr": "200",
        "ord_qty": "1",
        "nccs_qty": "1",
        "ord_dt": "20260611",
        "ord_tmd": "230000",
    }

    out = _normalize_kis_order(row, market="us")
    assert out["expiry_reason"] is None
```
- [ ] Run, expect failure: `uv run pytest tests/services/action_report/snapshot_backed/test_pending_orders_collector.py -v -k "expiry or reason"` → expected: `KeyError: 'expiry_reason'`.
- [ ] Minimal impl — add the import to `pending_orders.py` (after line 46's `from app.services.action_report.snapshot_backed.collectors._base import (...)` import group):
```python
from app.services.brokers.kis.live_order_expiry import kr_day_order_expiry
```
- [ ] Minimal impl — replace `_kis_expected_expiry` (lines 249-254) with:
```python
def _kis_expected_expiry(
    placed_at: dt.datetime | None, *, market: str, side: str
) -> tuple[str | None, str | None]:
    """(ISO expiry, categorical reason) for a KR pending order via the shared helper.

    ROB-671: delegates to the single stdlib-only computer so the collector and
    the send-path (kis_live_ledger) agree. US/crypto have no NXT session → None.
    The downgrade flag is left off here (the collector is a read surface, not a
    live TTL decision); by conservative default the value stays 20:00 KST.
    """
    if market != "kr" or placed_at is None:
        return None, None
    return kr_day_order_expiry(accepted_at=placed_at, side=side)
```
- [ ] Minimal impl — in `_normalize_kis_order`, compute side first and pass it. Replace the `placed_at = _kis_placed_at(row)` + `return {...}` region (lines 273-292) so `side` is computed once and reused, and both keys are emitted:
```python
    placed_at = _kis_placed_at(row)
    side = _normalize_kis_side(row)
    expiry_iso, expiry_reason = _kis_expected_expiry(
        placed_at, market=market, side=side
    )
    return {
        "target_ref": {
            "type": "broker_order",
            "broker": "kis",
            "id": order_id or "",
            "raw": dict(row),
        },
        "symbol": symbol,
        "side": side,
        "price": price,
        "quantity": quantity,
        "remaining_quantity": remaining_raw,
        "placed_at": placed_at.isoformat() if placed_at is not None else None,
        "expected_expiry": expiry_iso,
        "expiry_reason": expiry_reason,
        # KR/US use session expiry handled by the broker; classifier handles
        # session-based gating, so the collector never flags stale here.
        "stale": False,
        "market": market,
    }
```
- [ ] Minimal impl — add `"expiry_reason": None,` to the Upbit normalized dict in `_normalize_upbit_order` (after the `"expected_expiry": None,` at line 355) so the crypto row carries the key symmetrically:
```python
        "expected_expiry": None,
        "expiry_reason": None,
```
- [ ] Run the full collector suite, expect pass: `uv run pytest tests/services/action_report/snapshot_backed/test_pending_orders_collector.py -v` → expected: all pass (including the unchanged 20:00 default and US-None assertions).
- [ ] Commit: `git add app/services/action_report/snapshot_backed/collectors/pending_orders.py tests/services/action_report/snapshot_backed/test_pending_orders_collector.py && git commit -m "ROB-671: collector delegates expiry to shared helper + emits expiry_reason"`

---

## Task 5 — `expiry_reason` on `kis_live_get_order_history` rows

**Files:**
- Modify: `app/mcp_server/tooling/orders_modify_cancel.py` (imports line 25-28; `_normalize_kis_domestic_order` lines 154-241; `_normalize_kis_overseas_order` lines 372-423)
- Test: `tests/mcp_server/tooling/test_orders_history_expiry_reason.py` (create)

**Interfaces:**
- Consumes `kr_day_order_expiry`, `parse_kis_ordered_at` from `live_order_expiry`.
- Produces `_kr_history_expiry_reason(*, ordered_at: str, side: str) -> str | None` and `expiry_reason` key on the KR domestic normalized dict (categorical, or `None` if `ordered_at` unparseable); US overseas row carries `expiry_reason="us_day_order"` (placeholder — US has no NXT session; no timestamp claimed).

TDD steps:

- [ ] Write failing test `tests/mcp_server/tooling/test_orders_history_expiry_reason.py`:
```python
import pytest

from app.mcp_server.tooling.orders_modify_cancel import (
    _normalize_kis_domestic_order,
    _normalize_kis_overseas_order,
)


@pytest.mark.unit
def test_kr_domestic_regular_buy_reason_is_conservative():
    row = {
        "odno": "0011001100",
        "pdno": "005930",
        "sll_buy_dvsn_cd": "02",  # buy
        "ord_qty": "3",
        "rmn_qty": "3",
        "ord_unpr": "70000",
        "ord_dt": "20260703",
        "ord_tmd": "093015",
    }
    out = _normalize_kis_domestic_order(row)
    assert out["expiry_reason"] == "regular_buy_conservative_20_00"


@pytest.mark.unit
def test_kr_domestic_regular_sell_reason_is_nxt_carry():
    row = {
        "odno": "0011001101",
        "pdno": "005930",
        "sll_buy_dvsn_cd": "01",  # sell
        "ord_qty": "3",
        "rmn_qty": "3",
        "ord_unpr": "70000",
        "ord_dt": "20260703",
        "ord_tmd": "093015",
    }
    out = _normalize_kis_domestic_order(row)
    assert out["expiry_reason"] == "nxt_carry"


@pytest.mark.unit
def test_kr_domestic_unparseable_ordered_at_reason_none():
    row = {
        "odno": "0011001102",
        "pdno": "005930",
        "sll_buy_dvsn_cd": "02",
        "ord_qty": "3",
        "rmn_qty": "3",
        "ord_unpr": "70000",
        # no ord_dt / ord_tmd → ordered_at is " " → unparseable
    }
    out = _normalize_kis_domestic_order(row)
    assert out["expiry_reason"] is None


@pytest.mark.unit
def test_us_overseas_reason_is_us_day_order():
    row = {
        "odno": "US-9",
        "pdno": "AAPL",
        "sll_buy_dvsn_cd": "02",
        "ft_ord_qty": "1",
        "nccs_qty": "1",
        "ft_ord_unpr3": "200",
        "ord_dt": "20260703",
        "ord_tmd": "230000",
    }
    out = _normalize_kis_overseas_order(row)
    assert out["expiry_reason"] == "us_day_order"
```
- [ ] Run, expect failure: `uv run pytest tests/mcp_server/tooling/test_orders_history_expiry_reason.py -v` → expected: `KeyError: 'expiry_reason'`.
- [ ] Minimal impl — extend the import in `orders_modify_cancel.py` (line 26) to:
```python
from app.services.brokers.kis.live_order_expiry import (
    kr_day_order_expiry,
    parse_kis_ordered_at,
    row_has_cancel_evidence,
)
```
- [ ] Minimal impl — add the helper above `_normalize_kis_domestic_order` (before line 154):
```python
_US_DAY_ORDER_REASON = "us_day_order"


def _kr_history_expiry_reason(*, ordered_at: str, side: str) -> str | None:
    """Categorical session×side expiry reason for a KR order-history row.

    Read-path classification only (no 15:30 downgrade — that is a live send-path
    decision). Returns None when ``ordered_at`` cannot be parsed.
    """
    accepted_at = parse_kis_ordered_at(ordered_at)
    if accepted_at is None:
        return None
    return kr_day_order_expiry(accepted_at=accepted_at, side=side)[1]
```
- [ ] Minimal impl — in `_normalize_kis_domestic_order` return dict (lines 227-241), add the key. Insert after the `"filled_at": "",` line (line 239):
```python
        "expiry_reason": _kr_history_expiry_reason(ordered_at=ordered_at, side=side),
```
- [ ] Minimal impl — in `_normalize_kis_overseas_order` return dict (lines 406-423), add after the `"filled_at": "",` line (line 421):
```python
        "expiry_reason": _US_DAY_ORDER_REASON,
```
- [ ] Run, expect pass: `uv run pytest tests/mcp_server/tooling/test_orders_history_expiry_reason.py -v` → expected: all pass.
- [ ] Run the modify/cancel + history regression to prove no break: `uv run pytest tests/mcp_server/tooling/ -v -k "modify or cancel or history or normalize"` → expected: all pass.
- [ ] Commit: `git add app/mcp_server/tooling/orders_modify_cancel.py tests/mcp_server/tooling/test_orders_history_expiry_reason.py && git commit -m "ROB-671: emit categorical expiry_reason on KIS order-history rows"`

---

## Task 6 — Documentation (regular-sell NXT-carry + open buy-death cause)

**Files:**
- Modify: `CLAUDE.md` (add a short subsection under an existing KIS live section; keep additive)
- Modify: `docs/runbooks/kis-live-order-reconcile.md` (append a "Day-order expiry semantics (ROB-671)" section)
- Test: none (docs). Verified by `make lint` (docs are not linted; run the full targeted test set once more).

**Interfaces:** none (prose). Must state verbatim: (1) regular-session SELL carries to NXT (survives to 20:00, SOR cash sell) — so "is my order dead?" is not misjudged; (2) the regular-session BUY 15:30 death cause (session expiry vs D+2 unsettled-cash cancel, ROB-625 KRW variant) is UNCONFIRMED and the 15:30 downgrade is gated off behind `kis_regular_buy_unsettled_expiry_1530` pending a live measurement.

TDD steps:

- [ ] Minimal impl — add to `CLAUDE.md` a new subsection after the "KIS Live Order Fill-Evidence Gate (ROB-395)" block:
```markdown
### KIS Day-Order Expiry by Accept-Session × Side (ROB-671)

`kis_live_place_order` 응답의 `expected_expiry`/`expiry_reason` 및
`kis_live_get_order_history` 행의 `expiry_reason` 은 **접수 세션 × 매매구분**으로
결정된다. 순수 offline 분류기(`app/services/brokers/kis/live_order_expiry.py` —
stdlib only, 브로커/DB/네트워크/캘린더 import 없음, 주문 hot path 무네트워크 보장):

- 세션 창(KST, 마감 배타): premarket 08:00–08:50 / regular 09:00–15:30 /
  nxt_after 16:00–20:00 / 그 외 off.
- **정규장 SELL 은 NXT 로 연장**되어 20:00 KST 까지 유효(SOR 현금매도 NXT carry).
  → "내 매도주문이 죽었나?" 오판 금지. reason=`nxt_carry`.
- 정규장 BUY 는 **보수적 기본값 20:00 KST** (오늘 동작 유지), reason=
  `regular_buy_conservative_20_00`. ROB-657 이 관측한 정규장 매수 15:30 사멸은
  세션 만료가 아니라 **D+2 미결제(현금) 취소**(ROB-625 KRW variant)일 수 있어
  **원인 미확정**. 공격적 `15:30` 다운그레이드(reason=`regular_buy_unsettled_15_30`)
  는 구현되어 있으나 `KIS_REGULAR_BUY_UNSETTLED_EXPIRY_1530=true` (기본 off)
  게이트 뒤에 있으며, **라이브 측정으로 원인 확정 후에만** 활성화한다.
- premarket/nxt_after → 20:00(`nxt_carry`). off 창 접수 → 20:00(`unknown_session`).
- US(해외) 주문 history 행의 `expiry_reason` 은 `us_day_order` placeholder(NXT 없음).

reconcile 종료 분류(`classify_day_order_expiry`)는 변경 없음 — 여전히
evidence-first / fail-closed.
```
- [ ] Minimal impl — append to `docs/runbooks/kis-live-order-reconcile.md`:
```markdown
## Day-order expiry semantics (ROB-671)

`expected_expiry`/`expiry_reason` are derived offline from the accept-session ×
side (see `app/services/brokers/kis/live_order_expiry.py`). Operator notes:

- **Regular-session SELL survives to 20:00 KST (NXT carry, SOR cash sell).** Do
  not treat a still-open sell at 15:31–20:00 as expired; confirm via
  `remaining_qty`.
- **Regular-session BUY defaults to 20:00 KST (conservative).** The 15:30 death
  seen on regular-session buys is not yet proven to be session expiry — it may be
  a D+2 unsettled-cash cancel (ROB-625 KRW variant). The 15:30 downgrade lives
  behind `KIS_REGULAR_BUY_UNSETTLED_EXPIRY_1530` (default off).
- **Open follow-up (proposal 3, separable):** run a live measurement — place a
  regular-session buy funded by (a) settled cash vs (b) unsettled/미수 cash and
  record whether death occurs at 15:30. If session-driven, flip the flag; if
  settlement-driven, keep 20:00 and address at the funding layer. This is a
  measurement task, not code-blocking.
```
- [ ] Verify the full ROB-671 test set is green: `uv run pytest tests/services/brokers/kis/test_live_order_expiry_session.py tests/mcp_server/test_kis_live_place_order_routing_surface.py tests/mcp_server/tooling/test_orders_history_expiry_reason.py tests/services/action_report/snapshot_backed/test_pending_orders_collector.py -v` → expected: all pass.
- [ ] Run lint: `make lint` → expected: no new Ruff/ty errors in touched files.
- [ ] Commit: `git add CLAUDE.md docs/runbooks/kis-live-order-reconcile.md && git commit -m "ROB-671: document session×side expiry, regular-sell NXT-carry, open buy-death cause"`

---

## Self-Review

Spec-coverage mapping (acceptance criterion → task):

- **(a) `expected_expiry` + `expiry_reason` reflect accept-session × side (sell/premarket/nxt_after → 20:00; regular buy → 20:00 conservative default with reason noting uncertainty).** → Task 1 (classifier + `kr_day_order_expiry` returning `(iso, reason)`; conservative default; `REASON_REGULAR_BUY_CONSERVATIVE`), Task 3 (wired into the send response's `expected_expiry` + `expiry_reason`).
- **(b) Regular-session sell NXT-carry reflected in tool hints/docs.** → Task 3 (`_build_kr_routing_note` sell branch → "NXT carry … 20:00"), Task 6 (CLAUDE.md + runbook state regular-SELL survives to 20:00).
- **(c) `get_order_history` emits `expiry_reason`.** → Task 5 (`_normalize_kis_domestic_order` KR classifier + `_normalize_kis_overseas_order` US placeholder), with collector parity in Task 4.
- **(d) The 15:30 buy-death rule is implemented but gated off pending measurement, with the cause documented as open.** → Task 1 (`unsettled_regular_buy_downgrade` branch + `REASON_REGULAR_BUY_UNSETTLED_1530`, unit-tested), Task 2 (`kis_regular_buy_unsettled_expiry_1530: bool = False`), Task 3 (flag threaded, default keeps 20:00), Task 6 (open cause + separable live-measurement follow-up documented).
- **One shared helper (no duplicated computer).** → Task 1 defines it; Task 3 (send path), Task 4 (collector), Task 5 (history) all delegate to `kr_day_order_expiry`.
- **Offline / disjoint from ROB-668.** → Global Constraints; classifier lives in `live_order_expiry.py` (stdlib-only), `market_calendar.py`/`market_session.py` untouched.

## Out of scope

- **Regular-session BUY `expected_expiry` is deliberately NOT set to a literal 15:30 in this PR.** By conservative default it stays 20:00 KST (matching today) with `REASON_REGULAR_BUY_CONSERVATIVE` plus a 15:30 death-risk note surfaced in `routing.note` — so acceptance criterion (a) is only *partially* literal for regular buys by design. The literal 15:30 downgrade is implemented but gated off (`kis_regular_buy_unsettled_expiry_1530=False`) pending the proposal-3 live measurement; a reviewer should expect the regular-buy `expected_expiry` to read 20:00 (not 15:30) until that flag is flipped.
- The live measurement itself (proposal 3): placing settled vs unsettled regular-session buys to confirm the 15:30 death cause. Documented as a separable, non-code-blocking follow-up; flipping `kis_regular_buy_unsettled_expiry_1530=true` is deferred until that measurement lands.
- Toss / Upbit / Binance / Kiwoom expiry semantics — Upbit/crypto and US remain `expiry_reason=None`/`us_day_order` placeholders; Toss is ROB-668's territory and explicitly not edited.
- Changing the reconcile terminal classifier (`classify_day_order_expiry`) or the `nxt_session_closed` NXT gate — unchanged; this plan only adds send/read expiry *hints*, not terminal booking.
- New DB columns / persistence of `expiry_reason` — response-only, migration-0.
- Frontend surfacing of `expiry_reason` in `/invest` — no UI change in this plan.
```
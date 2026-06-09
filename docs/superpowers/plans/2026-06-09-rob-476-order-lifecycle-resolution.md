# ROB-476 — 주문 lifecycle 해소 + 라우팅 가시성 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (A) 마감 지난 미체결 KRX day order가 reconcile에서 영구 `pending`으로 남는 버그를 fail-closed로 `expired` 해소하고, (B) `place_order` 응답에 order_validity·routing·expected_expiry·broker_exchange를 정직하게 surface한다.

**Architecture:** Part A는 순수 stdlib 만료 분류기(`classify_day_order_expiry`)를 신설하고 `_reconcile_one_ledger_row`의 PENDING 분기에 배선한다. 시계열 판정은 기존 `kr_market_data_state`(XKRX 캘린더)를 reconcile 호출부에서 `market_closed: bool`로 계산해 분류기에 주입(분류기는 stdlib-only 유지, fill_evidence.py와 동일 철학). Part B는 `_record_kis_live_order` 응답에 비영속 필드를 추가. status는 CHECK 없는 Text → `expired` 추가는 migration-0.

**Tech Stack:** Python 3.13, SQLAlchemy(async), pytest/pytest-asyncio, exchange_calendars(XKRX, 간접).

**Migration:** 0.

> **NXT 이월 fail-closed 규칙(중요)**: PENDING 분기는 사실상 **당일** 주문에서만 발생한다(`_fetch_live_daily_rows`가 today만 조회 → 전일 주문은 verdict NONE→cancelled로 이미 해소). 따라서 time-guard는 당일 마감-후를 다뤄야 한다. NXT 이월 위험은 **broker 상태 토큰이 우선(evidence-first)**으로 흡수한다: 브로커가 주문을 live(접수/정상)로 보고하면 시계와 무관하게 `pending` 유지. 시계-기반 만료는 상태 토큰이 비정보적일 때만 발동. 정확한 KIS 상태 문자열은 fill_evidence.py와 동일하게 **operator read-only smoke로 확정**해야 하므로 토큰 목록은 보수적으로 두고 주석에 명시한다.

---

### Task 1: 순수 만료 분류기 `classify_day_order_expiry`

**Files:**
- Create: `app/services/brokers/kis/live_order_expiry.py`
- Test: `tests/services/brokers/kis/test_live_order_expiry.py`

stdlib-only. broker daily-order rows + order_no + `market_closed` 불린을 받아
`"expired"` 또는 `"pending"` 반환.

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/services/brokers/kis/test_live_order_expiry.py
from app.services.brokers.kis.live_order_expiry import classify_day_order_expiry


def _row(order_no="0011001100", prcs="", rvse=""):
    return {"odno": order_no, "prcs_stat_name": prcs, "rvse_cncl_dvsn_cd": rvse}


def test_live_status_token_stays_pending_even_when_market_closed():
    rows = [_row(prcs="접수완료")]
    assert classify_day_order_expiry(
        rows=rows, order_no="0011001100", market_closed=True
    ) == "pending"


def test_terminal_status_token_is_expired():
    rows = [_row(prcs="취소")]
    assert classify_day_order_expiry(
        rows=rows, order_no="0011001100", market_closed=False
    ) == "expired"


def test_time_guard_expires_when_market_closed_and_no_status():
    rows = [_row(prcs="")]
    assert classify_day_order_expiry(
        rows=rows, order_no="0011001100", market_closed=True
    ) == "expired"


def test_stays_pending_when_market_open_and_no_status():
    rows = [_row(prcs="")]
    assert classify_day_order_expiry(
        rows=rows, order_no="0011001100", market_closed=False
    ) == "pending"


def test_no_matching_row_stays_pending():
    # No row for this order_no → not our branch's job; stay pending (caller
    # already routes NONE-verdict elsewhere).
    rows = [_row(order_no="9999999999", prcs="")]
    assert classify_day_order_expiry(
        rows=rows, order_no="0011001100", market_closed=True
    ) == "pending"
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/brokers/kis/test_live_order_expiry.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.brokers.kis.live_order_expiry`

- [ ] **Step 3: 분류기 구현**

```python
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
intentionally conservative.
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
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/services/brokers/kis/test_live_order_expiry.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: 커밋**

```bash
git add app/services/brokers/kis/live_order_expiry.py tests/services/brokers/kis/test_live_order_expiry.py
git commit -m "feat(ROB-476): pure day-order expiry classifier (fail-closed, NXT-aware)"
```

---

### Task 2: lifecycle 매핑에 `expired` 추가

**Files:**
- Modify: `app/mcp_server/tooling/kis_live_ledger.py:36-45` (`_STATUS_TO_LIFECYCLE`)
- Test: `tests/mcp_server/test_kis_live_status_lifecycle.py`

`status="expired"`는 터미널이며 저널 부수효과가 없다. lifecycle_state 어휘
(ROB-100 contract)는 `cancelled`(터미널)로 매핑해 기존 어휘를 벗어나지 않는다.
정확한 구분은 `status` 컬럼이 보유.

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/mcp_server/test_kis_live_status_lifecycle.py
from app.mcp_server.tooling.kis_live_ledger import _status_to_lifecycle


def test_expired_maps_to_terminal_cancelled_lifecycle():
    assert _status_to_lifecycle("expired") == "cancelled"
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/mcp_server/test_kis_live_status_lifecycle.py -v`
Expected: FAIL — 현재 `expired`는 기본값 `"anomaly"`로 매핑됨

- [ ] **Step 3: 매핑 추가**

`_STATUS_TO_LIFECYCLE`에 항목 추가:

```python
    "cancelled": "cancelled",
    "expired": "cancelled",  # ROB-476 — terminal, no journal side-effect
    "anomaly": "anomaly",
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/mcp_server/test_kis_live_status_lifecycle.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add app/mcp_server/tooling/kis_live_ledger.py tests/mcp_server/test_kis_live_status_lifecycle.py
git commit -m "feat(ROB-476): map expired status to terminal cancelled lifecycle"
```

---

### Task 3: reconcile PENDING 분기에 만료 분류기 배선

**Files:**
- Modify: `app/mcp_server/tooling/kis_live_ledger.py` — imports + `_reconcile_one_ledger_row` PENDING 분기(line 433-435)
- Test: `tests/mcp_server/test_kis_live_reconcile_expiry.py`

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/mcp_server/test_kis_live_reconcile_expiry.py
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server.tooling import kis_live_ledger as mod
from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
    FillEvidence,
    FillVerdict,
)


def _pending_evidence():
    return FillEvidence(
        verdict=FillVerdict.PENDING, filled_qty=None, avg_price=None,
        category=None, reason_code="pending", detail="",
    )


def _row():
    return SimpleNamespace(
        id=1, order_no="0011001100", symbol="005930", side="buy",
        instrument_type="equity_kr", fee=0, currency="KRW",
    )


@pytest.mark.asyncio
async def test_pending_after_close_marks_expired_when_applied():
    rows = [{"odno": "0011001100", "prcs_stat_name": ""}]
    with patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=rows)), \
         patch.object(mod, "classify_fill_evidence", return_value=_pending_evidence()), \
         patch.object(mod, "kr_market_data_state",
                      return_value=mod.DATA_STATE_MARKET_CLOSED), \
         patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd:
        out = await mod._reconcile_one_ledger_row(_row(), dry_run=False)
    assert out["verdict"] == "expired"
    assert out["action"] == "marked_expired"
    upd.assert_awaited_once()
    assert upd.call_args.kwargs["status"] == "expired"


@pytest.mark.asyncio
async def test_pending_after_close_dry_run_does_not_write():
    rows = [{"odno": "0011001100", "prcs_stat_name": ""}]
    with patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=rows)), \
         patch.object(mod, "classify_fill_evidence", return_value=_pending_evidence()), \
         patch.object(mod, "kr_market_data_state",
                      return_value=mod.DATA_STATE_MARKET_CLOSED), \
         patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd:
        out = await mod._reconcile_one_ledger_row(_row(), dry_run=True)
    assert out["verdict"] == "expired"
    assert out["action"] == "would_mark_expired"
    upd.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_while_market_open_stays_noop_pending():
    rows = [{"odno": "0011001100", "prcs_stat_name": ""}]
    with patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=rows)), \
         patch.object(mod, "classify_fill_evidence", return_value=_pending_evidence()), \
         patch.object(mod, "kr_market_data_state",
                      return_value=mod.DATA_STATE_FRESH), \
         patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd:
        out = await mod._reconcile_one_ledger_row(_row(), dry_run=False)
    assert out["verdict"] == "pending"
    assert out["action"] == "noop_pending"
    upd.assert_not_awaited()
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/mcp_server/test_kis_live_reconcile_expiry.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'kr_market_data_state'` (아직 import 안 됨)

- [ ] **Step 3: import 추가**

`kis_live_ledger.py` 상단 import 블록에 추가:

```python
from app.mcp_server.tooling.market_session import (
    DATA_STATE_FRESH,
    DATA_STATE_MARKET_CLOSED,
    kr_market_data_state,
)
from app.services.brokers.kis.live_order_expiry import classify_day_order_expiry
```

(`DATA_STATE_FRESH`는 테스트 가독성용으로만 노출 — 미사용이면 ruff가 잡으니 실제로는
`DATA_STATE_MARKET_CLOSED`와 `kr_market_data_state`만 import하고, 테스트는
`mod.DATA_STATE_FRESH` 대신 문자열 `"fresh"`를 patch 반환값으로 사용해도 됨. 구현 시
미사용 import는 제거한다 — 메모리 교훈: CI lint는 app/ + tests/ 둘 다.)

- [ ] **Step 4: PENDING 분기 교체**

`_reconcile_one_ledger_row`의 기존:

```python
    if evidence.verdict == FillVerdict.PENDING:
        base["action"] = "noop_pending"
        return base
```

를 다음으로 교체:

```python
    if evidence.verdict == FillVerdict.PENDING:
        market_closed = kr_market_data_state() == DATA_STATE_MARKET_CLOSED
        expiry = classify_day_order_expiry(
            rows=rows, order_no=order_no, market_closed=market_closed
        )
        if expiry == "expired":
            base["verdict"] = "expired"
            base["action"] = (
                "marked_expired" if not dry_run else "would_mark_expired"
            )
            if not dry_run:
                await _update_ledger_outcome(ledger_id=row.id, status="expired")
            return base
        base["action"] = "noop_pending"
        return base
```

- [ ] **Step 5: 통과 확인**

Run: `uv run pytest tests/mcp_server/test_kis_live_reconcile_expiry.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: 커밋**

```bash
git add app/mcp_server/tooling/kis_live_ledger.py tests/mcp_server/test_kis_live_reconcile_expiry.py
git commit -m "feat(ROB-476): resolve expired day orders in reconcile PENDING branch"
```

---

### Task 4: place_order 응답 라우팅/만료 surface (Part B)

**Files:**
- Modify: `app/mcp_server/tooling/kis_live_ledger.py` — helper 2개 추가 + `_record_kis_live_order` 응답(line 219-242)
- Test: `tests/mcp_server/test_kis_live_place_order_routing_surface.py`

- [ ] **Step 1: 실패 테스트 작성 (순수 헬퍼)**

```python
# tests/mcp_server/test_kis_live_place_order_routing_surface.py
import datetime

from app.mcp_server.tooling.kis_live_ledger import (
    _expected_krx_expiry,
    _extract_broker_exchange,
)

KST = datetime.timezone(datetime.timedelta(hours=9))


def test_expected_krx_expiry_is_1530_kst_of_send_date():
    now = datetime.datetime(2026, 6, 9, 9, 43, tzinfo=KST)
    assert _expected_krx_expiry(now) == "2026-06-09T15:30:00+09:00"


def test_extract_broker_exchange_present():
    raw = {"output": {"EXCG_ID_DVSN_CD": "KRX"}}
    assert _extract_broker_exchange(raw) == "KRX"


def test_extract_broker_exchange_absent_is_none():
    assert _extract_broker_exchange({"output": {}}) is None
    assert _extract_broker_exchange({}) is None
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/mcp_server/test_kis_live_place_order_routing_surface.py -v`
Expected: FAIL — `ImportError: cannot import name '_expected_krx_expiry'`

- [ ] **Step 3: 헬퍼 구현**

`kis_live_ledger.py` (예: `_record_kis_live_order` 위)에 추가:

```python
_BROKER_EXCHANGE_KEYS = ("EXCG_ID_DVSN_CD", "excg_id_dvsn_cd", "exg_id_dvsn_cd")


def _expected_krx_expiry(now: datetime.datetime) -> str | None:
    """KRX day-order expiry = 15:30 KST of the send date (ISO 8601), or None."""
    try:
        kst = datetime.timezone(datetime.timedelta(hours=9))
        local = now.astimezone(kst)
        close = local.replace(hour=15, minute=30, second=0, microsecond=0)
        return close.isoformat()
    except (ValueError, OverflowError):
        return None


def _extract_broker_exchange(execution_result: dict[str, Any]) -> str | None:
    """Read the broker-reported exchange factually; None if absent (no fabrication)."""
    output = execution_result.get("output") or {}
    for source in (execution_result, output):
        for key in _BROKER_EXCHANGE_KEYS:
            val = source.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
    return None
```

- [ ] **Step 4: 응답 dict에 필드 추가**

`_record_kis_live_order`의 `return {...}` (line 219-242)에서 `"message"` 항목 앞에
추가:

```python
        "order_validity": "day",
        "routing": {
            "requested_venue": "auto",
            "note": "SOR auto-route (KRX; NXT-eligible)",
        },
        "expected_expiry": _expected_krx_expiry(now_kst()),
        "broker_exchange": _extract_broker_exchange(execution_result),
```

(`now_kst`는 이 모듈에서 이미 import됨 — line 293/322에서 사용 중.)

- [ ] **Step 5: 통과 확인**

Run: `uv run pytest tests/mcp_server/test_kis_live_place_order_routing_surface.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: 응답 통합 테스트 (save 패치)**

같은 테스트 파일에 추가:

```python
import pytest
from unittest.mock import AsyncMock, patch

from app.mcp_server.tooling import kis_live_ledger as mod


@pytest.mark.asyncio
async def test_place_order_response_surfaces_routing_fields():
    execution_result = {
        "odno": "0011001100", "ord_tmd": "094300", "rt_cd": "0",
        "msg1": "정상", "output": {"EXCG_ID_DVSN_CD": "KRX"},
    }
    dry_run_result = {"price": 209000, "quantity": 2, "estimated_value": 418000}
    with patch.object(mod, "_save_kis_live_order_ledger", AsyncMock(return_value=42)):
        resp = await mod._record_kis_live_order(
            normalized_symbol="005930", market_type="equity_kr", side="buy",
            order_type="limit", dry_run_result=dry_run_result,
            execution_result=execution_result, reason=None, exit_reason=None,
            thesis="t", strategy="s", target_price=None, stop_loss=None,
            min_hold_days=None, notes=None, indicators_snapshot=None,
        )
    assert resp["order_validity"] == "day"
    assert resp["routing"]["requested_venue"] == "auto"
    assert resp["broker_exchange"] == "KRX"
    assert resp["expected_expiry"].endswith("15:30:00+09:00")
```

- [ ] **Step 7: 통과 확인**

Run: `uv run pytest tests/mcp_server/test_kis_live_place_order_routing_surface.py -v`
Expected: PASS (4 passed)

- [ ] **Step 8: 커밋**

```bash
git add app/mcp_server/tooling/kis_live_ledger.py tests/mcp_server/test_kis_live_place_order_routing_surface.py
git commit -m "feat(ROB-476): surface order_validity/routing/expected_expiry/broker_exchange in place_order response"
```

---

### Task 5: 도구 설명 + 런북 (expired verdict + 라우팅)

**Files:**
- Modify: `app/mcp_server/tooling/orders_kis_variants.py` (reconcile desc ~554-560)
- Modify: `docs/runbooks/kis-live-order-reconcile.md`
- Test: `tests/mcp_server/test_kis_live_expired_docs.py`

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/mcp_server/test_kis_live_expired_docs.py
import inspect

from app.mcp_server.tooling import orders_kis_variants as mod


def test_reconcile_desc_mentions_expired():
    src = inspect.getsource(mod)
    assert "expired" in src
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/mcp_server/test_kis_live_expired_docs.py -v`
Expected: FAIL

- [ ] **Step 3: reconcile 설명에 expired 추가**

`kis_live_reconcile_orders` 설명 문자열에 추가(연결 스타일 유지):

```
                "Stale unfilled day orders are resolved to 'expired' once the "
                "KRX session has closed (fail-closed: a live broker status keeps "
                "them pending in case of NXT carryover). "
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/mcp_server/test_kis_live_expired_docs.py -v`
Expected: PASS

- [ ] **Step 5: 런북 업데이트**

`docs/runbooks/kis-live-order-reconcile.md` Verdicts 섹션에 추가:

```markdown
- `expired`: KRX 마감을 지난 미체결 day order. reconcile이 `status="expired"`로
  해소(영구 pending 방지). **Fail-closed**: 브로커가 주문을 live(접수/정상)로
  보고하면 `expired`로 넘기지 않고 `pending` 유지(SOR 주문이 NXT 세션에서
  살아있을 수 있음). 정확한 KIS 상태 문자열은 operator read-only smoke로 확정.
```

그리고 "Routing / lifecycle visibility (ROB-476)" 섹션 신설:

```markdown
## Routing / lifecycle visibility (ROB-476)

`place_order` 응답은 라우팅/만료 컨텍스트를 surface한다:
- `order_validity`: 항상 `"day"` (현재 day order만 지원; NXT/TIF는 ROB-463).
- `routing.requested_venue`/`note`: SOR auto-route (KRX; NXT-eligible).
- `expected_expiry`: 주문일 KRX 마감(15:30 KST) ISO 시각.
- `broker_exchange`: 브로커가 거래소 필드를 반환할 때만 표기(없으면 `null`,
  날조 없음).

> **NXT 세션 이월**: SOR-routed day order가 KRX 마감 후 NXT에서 살아있는지는
> KIS 동작에 의존하며 **operator 확정 필요**(미상). 그래서 만료 해소는 fail-closed.
> ROB-463(NXT venue 파라미터 추가)과 보완관계.
```

- [ ] **Step 6: 커밋**

```bash
git add app/mcp_server/tooling/orders_kis_variants.py docs/runbooks/kis-live-order-reconcile.md tests/mcp_server/test_kis_live_expired_docs.py
git commit -m "docs(ROB-476): document expired verdict + routing/lifecycle visibility"
```

---

### Task 6: get_order_history가 expired 반영 확인

**Files:**
- Test: `tests/mcp_server/test_kis_live_order_history_expired.py` (확인 테스트)
- (필요 시) Modify: order-history surface 코드

`get_order_history`/ledger 조회는 `status` 컬럼을 그대로 읽으므로 `expired`가
자동 반영되어야 한다. 별도 매핑/필터가 `expired`를 누락시키지 않는지 확인.

- [ ] **Step 1: 조회 경로 확인**

Run: `grep -rn "status" app/mcp_server/tooling/ app/routers/alpaca_paper_ledger.py | grep -i "order_history\|ledger" | head`
그리고 ledger status를 화이트리스트로 필터하는 곳이 있으면(예: `in_(("filled",...))`)
`expired`를 누락하지 않는지 점검. 누락 필터가 있으면 `expired` 추가.

- [ ] **Step 2: 확인 테스트 작성 (조회 헬퍼가 status를 패스스루하는지)**

조회 함수가 존재하면 그 함수가 `status="expired"` row를 그대로 반환하는지 단언하는
테스트를 작성. (조회 경로가 ledger status를 가공 없이 노출하면 trivially 통과 —
이 경우 회귀 가드로만 둔다.)

```python
# tests/mcp_server/test_kis_live_order_history_expired.py
# 조회 헬퍼 이름은 Step 1에서 확인한 실제 함수로 대체.
# 예시(패스스루 가드): expired status가 화이트리스트 필터에 막히지 않는지.
from app.mcp_server.tooling import kis_live_ledger as mod


def test_expired_is_a_terminal_status_value():
    # expired는 lifecycle 매핑에 존재(터미널). 조회에서 누락 필터가 없는지의
    # 회귀 가드.
    assert mod._status_to_lifecycle("expired") == "cancelled"
```

- [ ] **Step 3: 통과 확인 + 커밋**

Run: `uv run pytest tests/mcp_server/test_kis_live_order_history_expired.py -v`
Expected: PASS

```bash
git add tests/mcp_server/test_kis_live_order_history_expired.py
git commit -m "test(ROB-476): guard expired status surfaces through order history"
```

---

### Task 7: 전체 검증

- [ ] **Step 1: 신규 테스트 전체**

Run:
```bash
uv run pytest tests/services/brokers/kis/test_live_order_expiry.py \
  tests/mcp_server/test_kis_live_status_lifecycle.py \
  tests/mcp_server/test_kis_live_reconcile_expiry.py \
  tests/mcp_server/test_kis_live_place_order_routing_surface.py \
  tests/mcp_server/test_kis_live_expired_docs.py \
  tests/mcp_server/test_kis_live_order_history_expired.py -v
```
Expected: 전부 PASS.

- [ ] **Step 2: 회귀 — 기존 reconcile/place_order 동작**

Run: `uv run pytest tests/ -k "kis_live" -m "not integration" -v`
Expected: 기존 테스트 PASS (FILLED/PARTIAL/NONE 분기 무변경; 응답에 필드만 추가).

- [ ] **Step 3: lint (app/ + tests/ 둘 다 — CI 동형)**

Run: `uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/`
Expected: clean.

---

## Self-Review 체크
- 스펙 Part A(만료 해소)=Task 1/2/3/6, Part B(라우팅 surface)=Task 4, 도구/런북=Task 5. 전부 커버.
- placeholder: Task 6 Step 1/2는 조회 함수명을 런타임 확인하도록 명시(미정 함수명 대신 grep 절차 제공) — 실행 가능한 구체 절차. 그 외 placeholder 없음.
- 타입 일관성: `classify_day_order_expiry(rows, order_no, market_closed)` → str("expired"|"pending") 단일 시그니처; reconcile 호출부/테스트 일치. `_expected_krx_expiry(now)->str|None`, `_extract_broker_exchange(dict)->str|None` 일관.
- fail-closed: LIVE 토큰 우선(NXT 이월 보호), 시계-기반은 토큰 비정보적일 때만. 부분체결 불변(PENDING 분기만 손댐).
- Migration 0 (status CHECK 없음 확인됨; 응답 필드 비영속; lifecycle는 기존 어휘 cancelled 재사용).

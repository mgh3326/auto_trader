# ROB-487 Reconcile 증거 윈도우 확장 + NXT-aware Day-Order Expiry 하드닝 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** kis_live reconcile의 두 가지 검증된 잠재 결함(오늘-only 증거 윈도우, 실 데이터에서 절대 발동 불가한 NXT 토큰 가드)을 실측 TTTC8001R 행 형태 기반으로 수정하고, FillVerdict.NONE 분기를 fail-closed로 만들고, UX/문서를 실측으로 정정한다.

**Architecture:** `app/services/brokers/kis/live_order_expiry.py`(순수 분류기)를 실 TTTC8001R 키(`rjct_qty`/`cncl_yn`/`orgn_odno`/`rmn_qty`) 기반으로 재작성하고 NXT 마감(20:00 KST) 술어를 추가한다. `app/mcp_server/tooling/kis_live_ledger.py`의 reconcile 커널은 증거 조회 윈도우를 주문일~오늘로 넓히고, PENDING 분기는 XKRX 분류기 대신 NXT 술어를 쓰며, NONE 분기는 윈도우 커버리지가 증명될 때만 terminal 마킹한다. 모든 변경은 ROB-395 accepted-only/evidence-gated 불변식을 유지한다(terminal 행 자동 re-open 없음).

**Tech Stack:** Python 3.13 + uv, pytest(+pytest-asyncio, unittest.mock), async SQLAlchemy(변경 없음), migration 0 (코드-only).

- 워크트리: `/Users/mgh3326/work/auto_trader.rob-487` (브랜치 `rob-487`, base `origin/main` = `26f7daee` — 플랜 코드 인용은 이 커밋 기준. 실행 시작 전 `git fetch --prune origin && git merge --ff-only origin/main`으로 최신화하고, upstream이 이 플랜의 수정 대상 파일을 건드렸으면 인용 발췌를 재확인할 것)
- 단일 PR, base `main`. **데이터 복구 Task 없음** (아래 전제 정정 참고).

---

## Verified Root Cause (2026-06-10 조사)

### ⚠️ 이슈 전제 정정 — 보고된 증상은 버그가 아님

이슈 본문의 전제("pending 주문들이 6/10 아침 reconcile에서 누락됨")는 **거짓으로 판명**되었다 (라이브 DB SELECT + 라이브 read-only TTTC8001R 프로브 2회로 검증):

- **2026-06-09 19:02:20~22 KST에 실행된 dry_run=False reconcile이 당시 열려 있던 모든 ledger 행을 이미 정상 해소했다.** KAI(047810) NXT 야간 체결을 book(ledger id 19 → `status=filled`, `filled_qty=2`, `avg=126000`, `trade_id=107`, `journal_id=64`)하고, 진짜 미체결 day order 7건(id 13,14,16,17,18,20,21)을 `expired`로 마킹했다.
- **expired 처리 7건 전부 브로커 증거와 일치**: 라이브 TTTC8001R 프로브(윈도우 20260609)에서 7건 모두 `tot_ccld_qty=0` + `rjct_qty=ord_qty(전량)`. 유실된 체결 없음.
- 따라서 6/10 09:03의 "Reconciled 0 live order(s)"는 **정확한 동작**이다: 후보 쿼리는 `status IN ('accepted','pending','partial')`만 보며(`kis_live_ledger.py:436-443`), 그 시각 non-terminal 행이 0건이었다(당일 신규 주문 id 22~25는 09:12:58 이후 생성).
- **데이터 복구 Task는 불필요하다.** 지금까지의 모든 terminal 마킹이 브로커 현실과 일치함이 확인됨.

### 스코프: 검증으로 확인된 잠재 결함 2건 + UX + 문서

이번 사건에서는 "운 좋게" 안 터졌지만, 적대적 검증에서 **실재가 확인된** 결함들:

1. **증거 윈도우가 오늘-only 하드코딩** — `_fetch_live_daily_rows`(`kis_live_ledger.py:294-311`)가 `INQR_STRT_DT = INQR_END_DT = 오늘`. **TTTC8001R은 주문일 기준 윈도우다**: 라이브 프로브에서 20260610 윈도우는 6/9 주문을 0건 반환했다. 즉 전일 주문을 익일에 reconcile하면 항상 `FillVerdict.NONE` → dry_run=False에서 무조건 `cancelled`(`kis_live_ledger.py:490-496`). 이 경로는 6/9 09:33에 id 1~8에 실제로 발동했고(프로브로 6/8 주문 전건 `rjct_qty=전량`/`ccld=0` 확인되어 결과적으로 올바른 마킹이었을 뿐), 전일 체결이 있었다면 복구 불가로 묻혔을 것이다.
2. **ROB-476 NXT 토큰 가드는 실 데이터에서 dead code** — 실 TTTC8001R 행에는 분류기가 읽는 키(`prcs_stat_name`, `rvse_cncl_dvsn_cd`, `rvse_cncl_dvsn_name`)가 **존재하지 않는다**(라이브 프로브로 키 리스트 확정). 그래서 모든 PENDING 행이 XKRX-only(15:30 마감) 시간 가드(`live_order_expiry.py:24-25,73-78` + `market_session.py:58-64`)로 떨어졌고, 6/9 19:02 실행은 NXT 마감(20:00)보다 58분 일찍 SOR 주문들을 expire했다(결과는 우연히 정확).
3. **`FillVerdict.NONE` 분기의 거짓 주석** — `kis_live_ledger.py:491-492`의 "checking live pending orders" 주석이 가리키는 교차확인은 코드에 존재하지 않는다.
4. **UX** — 후보 0건일 때 "Reconciled 0 live order(s)" 메시지가 누락으로 오인됨(`kis_live_ledger.py:618-620`).
5. **`_expected_krx_expiry` 모순** — 15:31:25 KST에 접수된 KAI 주문이 이미 과거인 `expected_expiry`(당일 15:30)를 받았다(`kis_live_ledger.py:167-175`).
6. **문서 허위** — `domestic_orders.py:540-555` docstring이 실재하지 않는 키(`rvse_cncl_dvsn_cd/name` 등)를 나열; 런북의 "NXT carry-over 미상" 노트는 이제 실측으로 확정됨.

### 라이브 프로브 실측 (테스트 fixture의 근거)

- 실 TTTC8001R 행 키(확정): `odno`, `orgn_odno`, `pdno`, `prdt_name`, `sll_buy_dvsn_cd_name`, `ord_qty`, `ord_unpr`, `tot_ccld_qty`, `rjct_qty`, `rmn_qty`, `cncl_yn`, `excg_id_dvsn_cd`, `ord_tmd`, `ord_dt` 등. **`prcs_stat_name`/`rvse_cncl_dvsn_cd`/`rvse_cncl_dvsn_name` 없음.**
- 미체결 SOR day order는 EOD에 `rjct_qty == ord_qty`(+`tot_ccld_qty=0`)로 나타난다 (6/8 8건 + 6/9 7건 전수 확인).
- 체결(`tot_ccld_qty>0`)은 **주문일 윈도우에서만** 보인다 (6/9 체결은 20260609 윈도우에만 존재).
- 페이지네이션이 모든 행을 정확히 2회 반환(32행/16 unique, 3개 윈도우에서 일관) — `fill_evidence._dedupe_rows`가 현재 보호 중. 원인 조사는 본 PR 스코프 밖(말미 follow-up).
- `rjct_qty`가 장중에 채워지는지 EOD 배치에서만 채워지는지는 **미확인** → expiry 룰은 20:00 가드 뒤에만 둔다.

### 불변식 (전 Task 공통)

- terminal 행 자동 re-open 금지: `_list_open_ledger_rows`(후보 쿼리)와 terminal 배제 로직은 변경하지 않는다 (ROB-395 accepted-only/evidence-gated 유지).
- `market_session.kr_market_data_state`는 **수정 금지** (ROB-464 read 도구들의 공유 분류기). reconcile 경로에서 import만 제거.
- 늦은 terminal 마킹은 무해(evidence-first booking — 체결이 있으면 expiry보다 먼저 book됨), 조기 마킹은 유해. 따라서 모든 가드는 fail-closed(pending 유지).

---

## Task 1: 증거 윈도우 확장 — `_order_date_kst` + `_fetch_live_daily_rows(start_date=...)`

**Files:**
- Create: `tests/mcp_server/test_kis_live_reconcile_window.py`
- Modify: `app/mcp_server/tooling/kis_live_ledger.py` (:21 import, :294-311 fetch, :458-460 커널 호출부)

### Steps

- [ ] **Write the failing tests** — 새 파일 `tests/mcp_server/test_kis_live_reconcile_window.py` 생성 (Task 1 시점에는 아래 내용 전체가 파일의 전부):

```python
# tests/mcp_server/test_kis_live_reconcile_window.py
"""ROB-487 — 증거 윈도우 확장 + NONE-verdict fail-closed + 빈 후보 UX.

TTTC8001R 은 '주문일' 기준 윈도우다 — 2026-06-10 라이브 read-only 프로브에서
20260610 윈도우에 6/9 주문이 0건이었다. 익일 reconcile 이 전일 체결을 보려면
INQR_STRT_DT 를 ledger 행의 주문일(created_at KST date)로 넓혀야 한다.
"""

import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server.tooling import kis_live_ledger as mod
from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
    FillEvidence,
    FillVerdict,
)

KST = datetime.timezone(datetime.timedelta(hours=9))
UTC = datetime.timezone.utc


def _ledger_row(created_at, trade_date=None):
    return SimpleNamespace(
        id=19,
        order_no="0029287200",
        symbol="047810",
        side="buy",
        instrument_type="equity_kr",
        fee=0,
        currency="KRW",
        created_at=created_at,
        trade_date=trade_date,
        thesis="t",
        strategy="s",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
        exit_reason=None,
        reason=None,
    )


# --- _order_date_kst ----------------------------------------------------------


def test_order_date_kst_converts_aware_utc_to_kst_date():
    # 6/9 15:31:25 KST 주문의 DB 저장형(UTC): 06:31:25Z
    row = _ledger_row(datetime.datetime(2026, 6, 9, 6, 31, 25, tzinfo=UTC))
    assert mod._order_date_kst(row) == datetime.date(2026, 6, 9)
    # KST 자정 경계: 6/9 16:30Z == 6/10 01:30 KST
    row = _ledger_row(datetime.datetime(2026, 6, 9, 16, 30, tzinfo=UTC))
    assert mod._order_date_kst(row) == datetime.date(2026, 6, 10)


def test_order_date_kst_naive_assumed_kst():
    # naive 는 KST 관례 (app/core/timezone.to_kst_naive 와 동일 가정)
    row = _ledger_row(datetime.datetime(2026, 6, 9, 15, 31, 25))
    assert mod._order_date_kst(row) == datetime.date(2026, 6, 9)


def test_order_date_kst_falls_back_to_trade_date():
    trade = datetime.datetime(2026, 6, 9, 15, 31, tzinfo=KST)
    row = _ledger_row(None, trade_date=trade)
    assert mod._order_date_kst(row) == datetime.date(2026, 6, 9)


def test_order_date_kst_none_when_underivable():
    assert mod._order_date_kst(_ledger_row(None)) is None


# --- _fetch_live_daily_rows window ---------------------------------------------


@pytest.mark.asyncio
async def test_fetch_live_daily_rows_widens_window_to_start_date():
    fake_client = AsyncMock()
    fake_client.inquire_daily_order_domestic = AsyncMock(return_value=[])
    with (
        patch.object(mod, "_create_live_kis_client", return_value=fake_client),
        patch.object(mod, "_today_yyyymmdd", return_value="20260610"),
    ):
        await mod._fetch_live_daily_rows(
            symbol="047810", order_no="0029287200", start_date="20260609"
        )
    kwargs = fake_client.inquire_daily_order_domestic.await_args.kwargs
    assert kwargs["start_date"] == "20260609"  # 주문일
    assert kwargs["end_date"] == "20260610"  # 오늘
    assert kwargs["is_mock"] is False


@pytest.mark.asyncio
async def test_fetch_live_daily_rows_defaults_to_today_window():
    fake_client = AsyncMock()
    fake_client.inquire_daily_order_domestic = AsyncMock(return_value=[])
    with (
        patch.object(mod, "_create_live_kis_client", return_value=fake_client),
        patch.object(mod, "_today_yyyymmdd", return_value="20260610"),
    ):
        await mod._fetch_live_daily_rows(symbol="047810", order_no="0029287200")
    kwargs = fake_client.inquire_daily_order_domestic.await_args.kwargs
    assert kwargs["start_date"] == "20260610"
    assert kwargs["end_date"] == "20260610"


@pytest.mark.asyncio
async def test_reconcile_passes_order_date_window_to_fetch():
    row = _ledger_row(datetime.datetime(2026, 6, 9, 6, 31, 25, tzinfo=UTC))
    filled = FillEvidence(
        FillVerdict.FILLED, Decimal("2"), Decimal("126000"), None, "filled", ""
    )
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=[])) as f,
        patch.object(mod, "classify_fill_evidence", return_value=filled),
    ):
        out = await mod._reconcile_one_ledger_row(row, dry_run=True)
    assert f.await_args.kwargs["start_date"] == "20260609"
    assert out["action"] == "would_book_filled"
```

- [ ] **Run test to verify it fails**:

```bash
cd /Users/mgh3326/work/auto_trader.rob-487
uv run pytest tests/mcp_server/test_kis_live_reconcile_window.py -v
```

기대: `_order_date_kst` 테스트 4건 → `AttributeError: <module 'app.mcp_server.tooling.kis_live_ledger'> does not have the attribute '_order_date_kst'`; fetch 테스트 2건 → `TypeError: _fetch_live_daily_rows() got an unexpected keyword argument 'start_date'`; 커널 테스트 1건 → `start_date` kwarg 부재로 `KeyError: 'start_date'`. 전건 FAIL/ERROR.

- [ ] **Write minimal implementation** — `app/mcp_server/tooling/kis_live_ledger.py` 3곳 수정.

(1) import (line 21) — 현재 코드:

```python
from app.core.timezone import now_kst
```

변경 후:

```python
from app.core.timezone import KST, now_kst
```

(2) `_today_yyyymmdd`와 `_fetch_live_daily_rows` (lines 294-311) — 현재 코드:

```python
def _today_yyyymmdd() -> str:
    return datetime.datetime.now().strftime("%Y%m%d")


async def _fetch_live_daily_rows(
    *, symbol: str, order_no: str | None
) -> list[dict[str, Any]]:
    """Fetch today's live daily-execution rows for a KR order (is_mock=False)."""
    kis = _create_live_kis_client()
    today = _today_yyyymmdd()
    rows = await kis.inquire_daily_order_domestic(
        start_date=today,
        end_date=today,
        stock_code=symbol,
        order_number=order_no or "",
        is_mock=False,
    )
    return rows or []
```

변경 후:

```python
def _today_yyyymmdd() -> str:
    return datetime.datetime.now().strftime("%Y%m%d")


def _order_date_kst(row: Any) -> datetime.date | None:
    """Ledger row's order date in KST (created_at first, trade_date fallback).

    Naive timestamps are assumed KST (app/core/timezone convention). Returns
    None when underivable — callers must then refuse terminal markings
    (fail-closed) because the evidence window cannot be proven to cover the
    order date.
    """
    for attr in ("created_at", "trade_date"):
        dt = getattr(row, attr, None)
        if isinstance(dt, datetime.datetime):
            if dt.tzinfo is None:
                return dt.date()
            return dt.astimezone(KST).date()
    return None


async def _fetch_live_daily_rows(
    *, symbol: str, order_no: str | None, start_date: str | None = None
) -> list[dict[str, Any]]:
    """Fetch live daily-execution rows for a KR order (is_mock=False).

    ROB-487: TTTC8001R is ORDER-DATE-windowed — a today-only window returns
    zero rows for prior-day orders (live-verified 2026-06-10: the 20260610
    window contained none of the 6/9 orders). Callers must pass the ledger
    row's order date as ``start_date`` so next-day reconciles can still see
    prior-day fills; ``end_date`` stays today.
    """
    kis = _create_live_kis_client()
    today = _today_yyyymmdd()
    rows = await kis.inquire_daily_order_domestic(
        start_date=start_date or today,
        end_date=today,
        stock_code=symbol,
        order_number=order_no or "",
        is_mock=False,
    )
    return rows or []
```

(3) `_reconcile_one_ledger_row` 호출부 (lines 458-460) — 현재 코드:

```python
    order_no = row.order_no
    rows = await _fetch_live_daily_rows(symbol=row.symbol, order_no=order_no)
    evidence = classify_fill_evidence(order_no=order_no, rows=rows)
```

변경 후:

```python
    order_no = row.order_no
    order_date = _order_date_kst(row)
    rows = await _fetch_live_daily_rows(
        symbol=row.symbol,
        order_no=order_no,
        start_date=order_date.strftime("%Y%m%d") if order_date else None,
    )
    evidence = classify_fill_evidence(order_no=order_no, rows=rows)
```

- [ ] **Run test to verify it passes** (기존 회귀 포함):

```bash
uv run pytest tests/mcp_server/test_kis_live_reconcile_window.py \
  tests/mcp_server/test_kis_live_reconcile_expiry.py \
  tests/mcp_server/tooling/test_kis_live_ledger.py -v
```

기대: 전건 PASS. (기존 `test_fetch_live_daily_rows_for_order`는 `start_date` 기본값 None→오늘이므로 행동 보존. 기존 expiry 커널 테스트의 `_row()`는 `created_at`이 없어 `_order_date_kst`→None→오늘 윈도우로 기존 행동 유지.) 주의: `test_kis_live_ledger.py`는 DB 필요 — 로컬에서 `docker compose up -d` 선행.

- [ ] **Commit**:

```bash
uv run ruff format app/ tests/ && uv run ruff check app/ tests/
git add -A && git commit -m "$(cat <<'EOF'
fix(ROB-487): widen reconcile evidence window to the order date

TTTC8001R is order-date-windowed (live-verified 2026-06-10: a today-only
window returns zero prior-day orders), so next-day reconciles could never
see prior-day fills. The kernel now queries from the ledger row's order
date (created_at in KST) through today.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 2: `FillVerdict.NONE` 분기 fail-closed — 거짓 주석 삭제 + 윈도우 커버리지 가드

**Files:**
- Modify: `app/mcp_server/tooling/kis_live_ledger.py` (:490-496 NONE 분기)
- Test: `tests/mcp_server/test_kis_live_reconcile_window.py` (테스트 추가)

### Steps

- [ ] **Write the failing tests** — `tests/mcp_server/test_kis_live_reconcile_window.py` 끝에 추가:

```python
# --- FillVerdict.NONE fail-closed ----------------------------------------------


@pytest.mark.asyncio
async def test_none_verdict_with_covered_window_marks_cancelled():
    # 윈도우가 주문일을 커버(start_date == 주문일)했고 행 부재 → cancelled 유지.
    row = _ledger_row(datetime.datetime(2026, 6, 9, 6, 31, 25, tzinfo=UTC))
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=[])),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd,
    ):
        out = await mod._reconcile_one_ledger_row(row, dry_run=False)
    assert out["action"] == "marked_cancelled"
    upd.assert_awaited_once()
    assert upd.call_args.kwargs["status"] == "cancelled"


@pytest.mark.asyncio
async def test_none_verdict_with_covered_window_dry_run_does_not_write():
    row = _ledger_row(datetime.datetime(2026, 6, 9, 6, 31, 25, tzinfo=UTC))
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=[])),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd,
    ):
        out = await mod._reconcile_one_ledger_row(row, dry_run=True)
    assert out["action"] == "would_mark_cancelled"
    upd.assert_not_awaited()


@pytest.mark.asyncio
async def test_none_verdict_without_order_date_refuses_terminal_mark():
    # (f) 주문일 도출 불가 → 윈도우 커버 증명 불가 → terminal 마킹 금지 (noop).
    row = _ledger_row(None)
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=[])),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd,
    ):
        out = await mod._reconcile_one_ledger_row(row, dry_run=False)
    assert out["action"] == "noop_window_uncovered"
    assert "window" in out["reason"]
    upd.assert_not_awaited()
```

- [ ] **Run test to verify it fails**:

```bash
uv run pytest tests/mcp_server/test_kis_live_reconcile_window.py -v -k "none_verdict"
```

기대: covered-window 2건은 현재 코드로도 PASS(행동 보존 고정), `test_none_verdict_without_order_date_refuses_terminal_mark`만 FAIL — 현재 NONE 분기는 무조건 `marked_cancelled`이므로 `assert out["action"] == "noop_window_uncovered"`에서 `AssertionError`.

- [ ] **Write minimal implementation** — `kis_live_ledger.py` NONE 분기 (lines 490-496) — 현재 코드:

```python
    if evidence.verdict == FillVerdict.NONE:
        # No daily-execution row. Distinguish still-open vs cancelled by checking
        # live pending orders; absence from both fill and pending => cancelled.
        base["action"] = "marked_cancelled" if not dry_run else "would_mark_cancelled"
        if not dry_run:
            await _update_ledger_outcome(ledger_id=row.id, status="cancelled")
        return base
```

변경 후 (거짓 주석 삭제 — pending-orders 교차확인은 존재한 적이 없음):

```python
    if evidence.verdict == FillVerdict.NONE:
        # ROB-487 fail-closed: terminal 'cancelled' is written only when the
        # queried TTTC8001R window provably covered the order date
        # (start_date == order date <= end_date == today). No pending-orders
        # cross-check exists. If coverage cannot be proven, noop with an
        # explicit reason — terminal rows are permanently excluded from
        # candidates, so a wrong mark would bury a later-confirmed fill.
        if order_date is None:
            base["action"] = "noop_window_uncovered"
            base["reason"] = (
                "order date underivable from ledger row; evidence window not "
                "proven to cover the order date — refusing terminal mark"
            )
            return base
        base["action"] = "marked_cancelled" if not dry_run else "would_mark_cancelled"
        if not dry_run:
            await _update_ledger_outcome(ledger_id=row.id, status="cancelled")
        return base
```

(`order_date`는 Task 1에서 함수 상단에 도입됨. 윈도우 확장 후에는 구조상 항상 커버되지만, 가드는 명시적 코드로 남긴다.)

- [ ] **Run test to verify it passes**:

```bash
uv run pytest tests/mcp_server/test_kis_live_reconcile_window.py \
  tests/mcp_server/test_kis_live_reconcile_expiry.py \
  tests/mcp_server/tooling/test_kis_live_ledger.py -v
```

기대: 전건 PASS (기존 expiry 테스트는 PENDING-verdict 경로라 영향 없음).

- [ ] **Commit**:

```bash
uv run ruff format app/ tests/ && uv run ruff check app/ tests/
git add -A && git commit -m "$(cat <<'EOF'
fix(ROB-487): fail-closed FillVerdict.NONE branch

Delete the false comment claiming a live pending-orders cross-check (it
never existed). Mark 'cancelled' only when the evidence window provably
covered the order date; otherwise noop with an explicit reason.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 3: NXT 마감(20:00 KST) 술어 `nxt_session_closed` 추가

`market_session.kr_market_data_state`(XKRX 15:30, ROB-464 소비자 다수)는 **수정 금지**. 별도의 순수 술어를 `live_order_expiry.py`에 추가한다 — 주문일의 20:00 KST 이후에만 True.

**Files:**
- Modify: `app/services/brokers/kis/live_order_expiry.py` (import + 술어 추가, 기존 분류기는 이 Task에서 변경하지 않음)
- Test: `tests/services/brokers/kis/test_live_order_expiry.py` (테스트 추가)

### Steps

- [ ] **Write the failing tests** — `tests/services/brokers/kis/test_live_order_expiry.py`의 import 블록을 수정하고 파일 끝에 추가.

import — 현재 코드 (lines 1-2):

```python
# tests/services/brokers/kis/test_live_order_expiry.py
from app.services.brokers.kis.live_order_expiry import classify_day_order_expiry
```

변경 후:

```python
# tests/services/brokers/kis/test_live_order_expiry.py
import datetime

from app.services.brokers.kis.live_order_expiry import (
    classify_day_order_expiry,
    nxt_session_closed,
)

KST = datetime.timezone(datetime.timedelta(hours=9))
```

파일 끝에 추가:

```python
# --- nxt_session_closed (ROB-487) ----------------------------------------------


def test_nxt_open_before_2000_kst_same_day():
    assert (
        nxt_session_closed(
            order_date=datetime.date(2026, 6, 9),
            now=datetime.datetime(2026, 6, 9, 19, 59, tzinfo=KST),
        )
        is False
    )


def test_nxt_closed_at_exactly_2000_kst():
    assert (
        nxt_session_closed(
            order_date=datetime.date(2026, 6, 9),
            now=datetime.datetime(2026, 6, 9, 20, 0, tzinfo=KST),
        )
        is True
    )


def test_nxt_closed_next_day_morning():
    assert (
        nxt_session_closed(
            order_date=datetime.date(2026, 6, 9),
            now=datetime.datetime(2026, 6, 10, 9, 3, tzinfo=KST),
        )
        is True
    )


def test_nxt_naive_now_assumed_kst():
    # naive now 는 KST 관례 (app/core/timezone.to_kst_naive 와 동일 가정)
    assert (
        nxt_session_closed(
            order_date=datetime.date(2026, 6, 9),
            now=datetime.datetime(2026, 6, 9, 19, 2),
        )
        is False
    )


def test_nxt_utc_aware_now_converted():
    # 6/9 11:30 UTC == 6/9 20:30 KST → closed
    assert (
        nxt_session_closed(
            order_date=datetime.date(2026, 6, 9),
            now=datetime.datetime(2026, 6, 9, 11, 30, tzinfo=datetime.timezone.utc),
        )
        is True
    )
```

- [ ] **Run test to verify it fails**:

```bash
uv run pytest tests/services/brokers/kis/test_live_order_expiry.py -v
```

기대: `ImportError: cannot import name 'nxt_session_closed' from 'app.services.brokers.kis.live_order_expiry'` (collection error).

- [ ] **Write minimal implementation** — `app/services/brokers/kis/live_order_expiry.py`.

import — 현재 코드 (lines 19-21):

```python
from __future__ import annotations

from typing import Any
```

변경 후:

```python
from __future__ import annotations

import datetime
from typing import Any
```

`_ORDER_NO_KEYS` 정의(line 23) 바로 앞에 추가:

```python
_KST = datetime.timezone(datetime.timedelta(hours=9))

# NXT(대체거래소) 세션 마감 — SOR day order는 이 시각까지 살아있을 수 있다.
NXT_CLOSE_KST = datetime.time(hour=20, minute=0)


def nxt_session_closed(*, order_date: datetime.date, now: datetime.datetime) -> bool:
    """True iff ``now`` is at/after the NXT close (20:00 KST) of ``order_date``.

    Naive ``now`` is assumed KST (app/core/timezone convention). Pure function:
    the caller injects ``now`` — no clock import here.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=_KST)
    close = datetime.datetime.combine(order_date, NXT_CLOSE_KST, tzinfo=_KST)
    return now.astimezone(_KST) >= close
```

- [ ] **Run test to verify it passes**:

```bash
uv run pytest tests/services/brokers/kis/test_live_order_expiry.py -v
```

기대: 기존 5건 + 신규 5건 전건 PASS.

- [ ] **Commit**:

```bash
uv run ruff format app/ tests/ && uv run ruff check app/ tests/
git add -A && git commit -m "$(cat <<'EOF'
fix(ROB-487): add pure NXT close (20:00 KST) session predicate

kr_market_data_state stays XKRX-by-design for ROB-464 read tools; the
reconcile expiry path needs an NXT-aware closed check instead.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 4: `classify_day_order_expiry` 실 TTTC8001R 형태 재작성 + 커널 NXT-aware 재배선

핵심 Task. 분류기 시그니처/행동과 커널 PENDING 분기가 결합되어 있으므로 한 Task에서 함께 바꾼다 (커밋 시점 green 유지).

신규 분류 규칙 (fail-closed, 전부 any-row 술어 — 합산 없음이라 페이지네이션 중복(실측: 모든 행 2회)에 멱등):
- **terminal-cancel → `"cancelled"`**: 매칭 행의 `cncl_yn` truthy, 또는 취소확인 행(`orgn_odno`==주문번호 && `sll_buy_dvsn_cd_name`에 '취소') — 브로커 취소 증거이므로 시간 무관.
- **terminal-expiry → `"expired"`**: `rjct_qty == ord_qty > 0` **단 `nxt_closed=True`일 때만** (`rjct_qty`가 장중에 채워지는지 미확인 — verdict notes 잔여 불확실성. 20:00 가드 뒤에만 적용).
- **live → `"pending"`**: `rmn_qty > 0` 포함, 그 외 전부 기본값 pending. **순수 time-guard(토큰 없음 + 마감 → expired)는 폐기** — 증거 없는 expiry 없음.
- `prcs_stat_name`/`rvse_cncl_dvsn_*` 키 의존 전부 제거 (실 행에 존재하지 않음).
- 주문번호 매칭은 `fill_evidence`와 동일한 leading-zero 정규화로 통일 (구 분류기의 exact-match 불일치 해소).

커널은 `kr_market_data_state` 대신 `nxt_session_closed(order_date, now_kst())`를 사용하고, 분류기가 `"cancelled"`/`"expired"`를 돌려주면 해당 status로 마킹한다. KRX 전용 주문도 20:00까지 보수적으로 대기 — evidence-first booking이라 늦은 마킹은 무해하다.

**Files:**
- Modify: `app/services/brokers/kis/live_order_expiry.py` (전면 재작성 — Task 3의 술어 포함)
- Modify: `app/mcp_server/tooling/kis_live_ledger.py` (:22-25 import 제거, :35 import 확장, :450-488 커널 PENDING 분기)
- Test: `tests/services/brokers/kis/test_live_order_expiry.py` (전면 재작성 — Task 3 술어 테스트 보존)
- Test: `tests/mcp_server/test_kis_live_reconcile_expiry.py` (전면 재작성 — 가상 `prcs_stat_name` fixture 폐기)

### Steps

- [ ] **Write the failing tests (1/2)** — `tests/services/brokers/kis/test_live_order_expiry.py`를 아래 **전체 내용**으로 교체 (Task 3에서 추가한 술어 테스트는 그대로 포함됨):

```python
# tests/services/brokers/kis/test_live_order_expiry.py
"""ROB-487 — 실 TTTC8001R 행 형태 기반 day-order expiry 분류 + NXT 마감 술어.

라이브 read-only 프로브(2026-06-10, 윈도우 20260608/09/10)로 확정된 실 행 키:
odno / orgn_odno / ord_qty / tot_ccld_qty / rjct_qty / rmn_qty / cncl_yn /
sll_buy_dvsn_cd_name / excg_id_dvsn_cd ... — prcs_stat_name 과
rvse_cncl_dvsn_cd / rvse_cncl_dvsn_name 은 존재하지 않는다.
미체결 SOR day order 는 EOD 에 rjct_qty == ord_qty (tot_ccld_qty == 0).
"""

import datetime

from app.services.brokers.kis.live_order_expiry import (
    classify_day_order_expiry,
    nxt_session_closed,
)

KST = datetime.timezone(datetime.timedelta(hours=9))


def _live_row(**overrides):
    """실측 TTTC8001R 행 형태 (KAI 047810 6/9 사례를 기본값으로)."""
    row = {
        "odno": "0029287200",
        "orgn_odno": "0000000000",
        "pdno": "047810",
        "prdt_name": "한국항공우주",
        "sll_buy_dvsn_cd_name": "매수",
        "ord_qty": "2",
        "ord_unpr": "126000",
        "tot_ccld_qty": "0",
        "rjct_qty": "0",
        "rmn_qty": "2",
        "cncl_yn": "N",
        "excg_id_dvsn_cd": "SOR",
        "ord_tmd": "153125",
    }
    row.update(overrides)
    return row


# --- nxt_session_closed (ROB-487) ----------------------------------------------


def test_nxt_open_before_2000_kst_same_day():
    assert (
        nxt_session_closed(
            order_date=datetime.date(2026, 6, 9),
            now=datetime.datetime(2026, 6, 9, 19, 59, tzinfo=KST),
        )
        is False
    )


def test_nxt_closed_at_exactly_2000_kst():
    assert (
        nxt_session_closed(
            order_date=datetime.date(2026, 6, 9),
            now=datetime.datetime(2026, 6, 9, 20, 0, tzinfo=KST),
        )
        is True
    )


def test_nxt_closed_next_day_morning():
    assert (
        nxt_session_closed(
            order_date=datetime.date(2026, 6, 9),
            now=datetime.datetime(2026, 6, 10, 9, 3, tzinfo=KST),
        )
        is True
    )


def test_nxt_naive_now_assumed_kst():
    # naive now 는 KST 관례 (app/core/timezone.to_kst_naive 와 동일 가정)
    assert (
        nxt_session_closed(
            order_date=datetime.date(2026, 6, 9),
            now=datetime.datetime(2026, 6, 9, 19, 2),
        )
        is False
    )


def test_nxt_utc_aware_now_converted():
    # 6/9 11:30 UTC == 6/9 20:30 KST → closed
    assert (
        nxt_session_closed(
            order_date=datetime.date(2026, 6, 9),
            now=datetime.datetime(2026, 6, 9, 11, 30, tzinfo=datetime.timezone.utc),
        )
        is True
    )


# --- classify_day_order_expiry ---------------------------------------------------


def test_unfilled_sor_before_nxt_close_stays_pending():
    # (b) 15:31~19:59 KST 미체결 SOR — 6/9 19:02 조기 expiry 재발 방지.
    rows = [_live_row(rjct_qty="0", rmn_qty="2")]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0029287200", nxt_closed=False)
        == "pending"
    )


def test_full_rjct_qty_after_nxt_close_is_expired():
    # (c) 20:00 이후 + rjct_qty == ord_qty > 0 (EOD 만료의 실측 형태).
    rows = [_live_row(ord_qty="2", rjct_qty="2", rmn_qty="0")]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0029287200", nxt_closed=True)
        == "expired"
    )


def test_full_rjct_qty_before_nxt_close_stays_pending():
    # rjct_qty 가 장중에 채워지는 시점 미확인 → 20:00 전에는 fail-closed.
    rows = [_live_row(ord_qty="2", rjct_qty="2", rmn_qty="0")]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0029287200", nxt_closed=False)
        == "pending"
    )


def test_partial_rjct_after_close_stays_pending():
    rows = [_live_row(ord_qty="10", rjct_qty="6", rmn_qty="0")]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0029287200", nxt_closed=True)
        == "pending"
    )


def test_no_informative_evidence_after_close_stays_pending():
    # 순수 time-guard 폐기 회귀: EOD 배치 전(rjct=0, rmn>0)이면 20:00 후에도 pending.
    rows = [_live_row(ord_qty="2", rjct_qty="0", rmn_qty="2")]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0029287200", nxt_closed=True)
        == "pending"
    )


def test_cncl_yn_truthy_is_cancelled_any_time():
    rows = [_live_row(cncl_yn="Y", rmn_qty="0")]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0029287200", nxt_closed=False)
        == "cancelled"
    )


def test_cancel_confirm_row_matched_via_orgn_odno():
    # (e) 취소확인 행: 새 odno + orgn_odno == 원주문 + '매수취소'.
    rows = [
        _live_row(rjct_qty="0", rmn_qty="0"),
        _live_row(
            odno="0029999999",
            orgn_odno="0029287200",
            sll_buy_dvsn_cd_name="매수취소",
            rmn_qty="0",
        ),
    ]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0029287200", nxt_closed=False)
        == "cancelled"
    )


def test_modify_confirm_row_is_not_cancel_evidence():
    rows = [
        _live_row(rjct_qty="0", rmn_qty="2"),
        _live_row(
            odno="0029999999",
            orgn_odno="0029287200",
            sll_buy_dvsn_cd_name="매수정정",
        ),
    ]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0029287200", nxt_closed=False)
        == "pending"
    )


def test_duplicated_pagination_rows_do_not_change_verdicts():
    # (d) 실측: 모든 행이 정확히 2회 반환(32행/16 unique) — any-row 술어라 멱등.
    expired_single = [_live_row(ord_qty="2", rjct_qty="2", rmn_qty="0")]
    for rows in (expired_single, expired_single * 2):
        assert (
            classify_day_order_expiry(
                rows=rows, order_no="0029287200", nxt_closed=True
            )
            == "expired"
        )
    cancel_single = [_live_row(cncl_yn="Y")]
    assert (
        classify_day_order_expiry(
            rows=cancel_single * 2, order_no="0029287200", nxt_closed=False
        )
        == "cancelled"
    )


def test_no_matching_row_stays_pending():
    # 해당 주문 행 없음 → 이 분기 책임 아님 (NONE-verdict 경로가 처리).
    rows = [_live_row(odno="9999999999", orgn_odno="0000000000")]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0029287200", nxt_closed=True)
        == "pending"
    )


def test_order_no_leading_zero_normalization_matches():
    # fill_evidence 와 동일한 정규화 — 구 분류기의 exact-match 불일치 해소.
    rows = [_live_row(ord_qty="2", rjct_qty="2")]
    assert (
        classify_day_order_expiry(rows=rows, order_no="29287200", nxt_closed=True)
        == "expired"
    )


def test_missing_order_no_stays_pending():
    assert (
        classify_day_order_expiry(rows=[_live_row()], order_no=None, nxt_closed=True)
        == "pending"
    )
```

- [ ] **Write the failing tests (2/2)** — `tests/mcp_server/test_kis_live_reconcile_expiry.py`를 아래 **전체 내용**으로 교체:

```python
# tests/mcp_server/test_kis_live_reconcile_expiry.py
"""ROB-476/ROB-487 — reconcile 커널: NXT-aware expiry + 실 row 형태 증거.

fixture 는 실 TTTC8001R 행 형태 (2026-06-10 라이브 read-only 프로브로 확정):
prcs_stat_name / rvse_cncl_dvsn_* 키는 존재하지 않는다. classify_fill_evidence /
classify_day_order_expiry / nxt_session_closed 는 실물을 사용한다 (self-fulfilling
mock 금지).
"""

import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server.tooling import kis_live_ledger as mod

KST = datetime.timezone(datetime.timedelta(hours=9))

_CREATED_0609 = datetime.datetime(2026, 6, 9, 15, 31, 25, tzinfo=KST)


def _ledger_row(created_at=_CREATED_0609):
    return SimpleNamespace(
        id=19,
        order_no="0029287200",
        symbol="047810",
        side="buy",
        instrument_type="equity_kr",
        fee=0,
        currency="KRW",
        created_at=created_at,
        trade_date=created_at,
        thesis="t",
        strategy="s",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
        exit_reason=None,
        reason=None,
    )


def _broker_row(**overrides):
    row = {
        "odno": "0029287200",
        "orgn_odno": "0000000000",
        "pdno": "047810",
        "sll_buy_dvsn_cd_name": "매수",
        "ord_qty": "2",
        "ord_unpr": "126000",
        "tot_ccld_qty": "0",
        "rjct_qty": "0",
        "rmn_qty": "2",
        "cncl_yn": "N",
        "excg_id_dvsn_cd": "SOR",
        "ord_tmd": "153125",
    }
    row.update(overrides)
    return row


def test_kernel_no_longer_uses_xkrx_session_classifier():
    # ROB-487: XKRX 15:30 분류기는 reconcile 경로에서 제거 — NXT 술어만 사용.
    assert not hasattr(mod, "kr_market_data_state")
    assert not hasattr(mod, "DATA_STATE_MARKET_CLOSED")
    assert hasattr(mod, "nxt_session_closed")


@pytest.mark.asyncio
async def test_unfilled_sor_during_nxt_session_stays_pending():
    # 6/9 19:02 KST 조기 expiry 재발 방지: NXT 마감(20:00) 전에는 expire 금지.
    rows = [_broker_row()]  # tot_ccld_qty=0, rjct_qty=0, rmn_qty=2 → 생존
    now = datetime.datetime(2026, 6, 9, 19, 2, tzinfo=KST)
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=rows)),
        patch.object(mod, "now_kst", return_value=now),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd,
    ):
        out = await mod._reconcile_one_ledger_row(_ledger_row(), dry_run=False)
    assert out["verdict"] == "pending"
    assert out["action"] == "noop_pending"
    upd.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_rjct_before_nxt_close_stays_pending():
    # rjct_qty 의 장중 채움 여부 미확인 → 20:00 전에는 fail-closed pending.
    rows = [_broker_row(rjct_qty="2", rmn_qty="0")]
    now = datetime.datetime(2026, 6, 9, 19, 59, tzinfo=KST)
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=rows)),
        patch.object(mod, "now_kst", return_value=now),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd,
    ):
        out = await mod._reconcile_one_ledger_row(_ledger_row(), dry_run=False)
    assert out["action"] == "noop_pending"
    upd.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_rjct_after_nxt_close_marks_expired():
    # (c) 20:00 이후 + rjct_qty == ord_qty 브로커 증거 → expired.
    rows = [_broker_row(rjct_qty="2", rmn_qty="0")]
    now = datetime.datetime(2026, 6, 9, 20, 5, tzinfo=KST)
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=rows)),
        patch.object(mod, "now_kst", return_value=now),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd,
    ):
        out = await mod._reconcile_one_ledger_row(_ledger_row(), dry_run=False)
    assert out["verdict"] == "expired"
    assert out["action"] == "marked_expired"
    upd.assert_awaited_once()
    assert upd.call_args.kwargs["status"] == "expired"


@pytest.mark.asyncio
async def test_full_rjct_after_nxt_close_dry_run_does_not_write():
    rows = [_broker_row(rjct_qty="2", rmn_qty="0")]
    now = datetime.datetime(2026, 6, 9, 20, 5, tzinfo=KST)
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=rows)),
        patch.object(mod, "now_kst", return_value=now),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd,
    ):
        out = await mod._reconcile_one_ledger_row(_ledger_row(), dry_run=True)
    assert out["verdict"] == "expired"
    assert out["action"] == "would_mark_expired"
    upd.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_rjct_evidence_after_close_stays_pending():
    # 순수 time-guard 폐기 회귀: 20:00 후라도 broker 증거 없으면 expire 금지.
    rows = [_broker_row(rjct_qty="0", rmn_qty="2")]
    now = datetime.datetime(2026, 6, 9, 20, 5, tzinfo=KST)
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=rows)),
        patch.object(mod, "now_kst", return_value=now),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd,
    ):
        out = await mod._reconcile_one_ledger_row(_ledger_row(), dry_run=False)
    assert out["action"] == "noop_pending"
    upd.assert_not_awaited()


@pytest.mark.asyncio
async def test_next_day_reconcile_books_prior_day_fill_via_widened_window():
    # (a) 6/9 주문을 6/10 아침에 reconcile: start_date=주문일 윈도우로 전일
    # 체결(tot_ccld_qty=2)을 보고 book 한다 — KAI 047810 실측 형태.
    fill_row = _broker_row(tot_ccld_qty="2", rmn_qty="0", avg_prvs="126000")
    now = datetime.datetime(2026, 6, 10, 9, 3, tzinfo=KST)
    with (
        patch.object(
            mod, "_fetch_live_daily_rows", AsyncMock(return_value=[fill_row])
        ) as fetch,
        patch.object(mod, "now_kst", return_value=now),
        patch.object(mod, "_save_order_fill", AsyncMock(return_value=107)) as m_fill,
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            AsyncMock(return_value={"journal_id": 64}),
        ),
        patch.object(mod, "_link_journal_to_fill", AsyncMock()),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd,
    ):
        out = await mod._reconcile_one_ledger_row(_ledger_row(), dry_run=False)
    assert fetch.await_args.kwargs["start_date"] == "20260609"
    assert out["verdict"] == "filled"
    assert out["action"] == "booked_filled"
    assert float(m_fill.await_args.kwargs["price"]) == 126000.0
    assert float(m_fill.await_args.kwargs["quantity"]) == 2.0
    assert upd.call_args.kwargs["status"] == "filled"
    assert upd.call_args.kwargs["filled_qty"] == Decimal("2")


@pytest.mark.asyncio
async def test_duplicated_pagination_rows_do_not_double_book():
    # (d) 실측 페이지네이션 중복(모든 행 2회) — fill_evidence._dedupe_rows 가
    # 보호함을 커널 경유로 회귀 고정 (2가 4로 이중계상되면 안 됨).
    fill_row = _broker_row(tot_ccld_qty="2", rmn_qty="0", avg_prvs="126000")
    rows = [fill_row, dict(fill_row)]
    now = datetime.datetime(2026, 6, 10, 9, 3, tzinfo=KST)
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=rows)),
        patch.object(mod, "now_kst", return_value=now),
        patch.object(mod, "_save_order_fill", AsyncMock(return_value=107)) as m_fill,
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            AsyncMock(return_value={"journal_id": 64}),
        ),
        patch.object(mod, "_link_journal_to_fill", AsyncMock()),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()),
    ):
        out = await mod._reconcile_one_ledger_row(_ledger_row(), dry_run=False)
    assert out["verdict"] == "filled"
    assert float(m_fill.await_args.kwargs["quantity"]) == 2.0  # 4가 아님


@pytest.mark.asyncio
async def test_cancel_confirm_row_marks_cancelled_via_orgn_odno():
    # (e) 취소확인 행(신규 odno + orgn_odno == 원주문 + '매수취소') → cancelled.
    # 브로커 취소 증거는 시간 가드 불요 — NXT 마감 전이라도 즉시.
    rows = [
        _broker_row(rmn_qty="0"),
        _broker_row(
            odno="0029999999",
            orgn_odno="0029287200",
            sll_buy_dvsn_cd_name="매수취소",
            rmn_qty="0",
        ),
    ]
    now = datetime.datetime(2026, 6, 9, 16, 0, tzinfo=KST)
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=rows)),
        patch.object(mod, "now_kst", return_value=now),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd,
    ):
        out = await mod._reconcile_one_ledger_row(_ledger_row(), dry_run=False)
    assert out["verdict"] == "cancelled"
    assert out["action"] == "marked_cancelled"
    upd.assert_awaited_once()
    assert upd.call_args.kwargs["status"] == "cancelled"
```

- [ ] **Run test to verify it fails**:

```bash
uv run pytest tests/services/brokers/kis/test_live_order_expiry.py \
  tests/mcp_server/test_kis_live_reconcile_expiry.py -v
```

기대: 분류기 테스트 → `TypeError: classify_day_order_expiry() got an unexpected keyword argument 'nxt_closed'`; 커널 테스트 → `test_kernel_no_longer_uses_xkrx_session_classifier` 는 `assert not hasattr(mod, "kr_market_data_state")` 에서 `AssertionError`, 나머지는 PENDING 분기가 아직 `kr_market_data_state()`(실제 시계)를 쓰므로 시간대에 따라 오동작/실패.

- [ ] **Write minimal implementation (1/2)** — `app/services/brokers/kis/live_order_expiry.py`를 아래 **전체 내용**으로 교체:

```python
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

_ORDER_NO_KEYS = ("odno", "ord_no")
_ORIGIN_ORDER_NO_KEYS = ("orgn_odno", "orgn_ord_no")
_SIDE_NAME_KEYS = ("sll_buy_dvsn_cd_name", "sll_buy_dvsn_name")
_CANCEL_FLAG_KEYS = ("cncl_yn",)
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
```

- [ ] **Write minimal implementation (2/2)** — `app/mcp_server/tooling/kis_live_ledger.py` 2곳 수정.

(1) import — 현재 코드 (lines 22-25, 35):

```python
from app.mcp_server.tooling.market_session import (
    DATA_STATE_MARKET_CLOSED,
    kr_market_data_state,
)
```

```python
from app.services.brokers.kis.live_order_expiry import classify_day_order_expiry
```

변경 후: `market_session` import 4줄을 **삭제**하고, `live_order_expiry` import를 확장:

```python
from app.services.brokers.kis.live_order_expiry import (
    classify_day_order_expiry,
    nxt_session_closed,
)
```

(2) PENDING 분기 — 현재 코드 (Task 1 적용 후의 `_reconcile_one_ledger_row` 내부, 원본 lines 476-488):

```python
    if evidence.verdict == FillVerdict.PENDING:
        market_closed = kr_market_data_state() == DATA_STATE_MARKET_CLOSED
        expiry = classify_day_order_expiry(
            rows=rows, order_no=order_no, market_closed=market_closed
        )
        if expiry == "expired":
            base["verdict"] = "expired"
            base["action"] = "marked_expired" if not dry_run else "would_mark_expired"
            if not dry_run:
                await _update_ledger_outcome(ledger_id=row.id, status="expired")
            return base
        base["action"] = "noop_pending"
        return base
```

변경 후:

```python
    if evidence.verdict == FillVerdict.PENDING:
        # ROB-487: SOR day order는 NXT 마감(20:00 KST)까지 살아있다. KRX 전용
        # 주문도 20:00까지 보수적으로 대기 — evidence-first booking이라 늦은
        # terminal 마킹은 무해하고, 조기 마킹(6/9 19:02 사례)은 유해하다.
        nxt_closed = order_date is not None and nxt_session_closed(
            order_date=order_date, now=now_kst()
        )
        expiry = classify_day_order_expiry(
            rows=rows, order_no=order_no, nxt_closed=nxt_closed
        )
        if expiry in ("expired", "cancelled"):
            base["verdict"] = expiry
            base["action"] = (
                f"marked_{expiry}" if not dry_run else f"would_mark_{expiry}"
            )
            if not dry_run:
                await _update_ledger_outcome(ledger_id=row.id, status=expiry)
            return base
        base["action"] = "noop_pending"
        return base
```

아울러 `_reconcile_one_ledger_row`의 docstring (원본 lines 453-457)을 갱신 — 현재 코드:

```python
    """Classify one accepted/pending order and apply journal mutation if filled.

    Pending -> noop. Cancelled/rejected (no matching row) -> ledger update only.
    Filled/partial -> book fill + journal from BROKER-confirmed qty/price.
    """
```

변경 후:

```python
    """Classify one accepted/pending order and apply journal mutation if filled.

    Pending -> noop, or expired/cancelled on broker evidence after NXT close
    (20:00 KST). NONE verdict -> cancelled only when the evidence window
    provably covered the order date (ROB-487 fail-closed). Filled/partial ->
    book fill + journal from BROKER-confirmed qty/price.
    """
```

- [ ] **Run test to verify it passes** (관련 스위트 전체):

```bash
uv run pytest tests/services/brokers/kis/test_live_order_expiry.py \
  tests/mcp_server/test_kis_live_reconcile_expiry.py \
  tests/mcp_server/test_kis_live_reconcile_window.py \
  tests/mcp_server/tooling/test_kis_live_ledger.py \
  tests/mcp_server/tooling/test_kis_live_reconcile_tool.py -v
```

기대: 전건 PASS. (`test_kis_live_ledger.py`의 `test_reconcile_pending_is_noop`은 빈 rows → 분류기 "pending"이라 시계와 무관하게 통과; `test_reconcile_filled_buy_books_fill_and_journal`은 FILLED 경로라 영향 없음.)

- [ ] **Commit**:

```bash
uv run ruff format app/ tests/ && uv run ruff check app/ tests/
git add -A && git commit -m "$(cat <<'EOF'
fix(ROB-487): rewrite day-order expiry for real TTTC8001R rows + NXT-aware guard

Real rows carry no prcs_stat_name/rvse_cncl_dvsn_* keys (live-verified
2026-06-10), so the ROB-476 token classifier was dead code and every
pending row fell to the XKRX 15:30 time-guard, expiring SOR orders 58
minutes before NXT close. New evidence-first rules: cncl_yn / orgn_odno
cancel-confirm -> cancelled; rjct_qty == ord_qty > 0 AND past NXT close
(20:00 KST) -> expired; otherwise fail-closed pending. The bare
time-guard is gone; the reconcile kernel now injects nxt_session_closed
instead of kr_market_data_state (market_session untouched for ROB-464
consumers).

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 5: 후보 0건 UX 메시지 구분

**Files:**
- Modify: `app/mcp_server/tooling/kis_live_ledger.py` (:612-621 반환부)
- Test: `tests/mcp_server/test_kis_live_reconcile_window.py` (테스트 추가)

### Steps

- [ ] **Write the failing tests** — `tests/mcp_server/test_kis_live_reconcile_window.py` 끝에 추가:

```python
# --- 빈 후보 UX 메시지 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_impl_empty_candidates_message_is_distinguishable():
    # ROB-487 UX: "Reconciled 0" 이 누락으로 오인되지 않도록 후보 0건을 구분.
    with patch.object(mod, "_list_open_ledger_rows", AsyncMock(return_value=[])):
        out = await mod.kis_live_reconcile_orders_impl(dry_run=True)
    assert out["success"] is True
    assert out["counts"] == {}
    assert "No open candidates (all ledger rows terminal)" in out["message"]


@pytest.mark.asyncio
async def test_impl_nonempty_keeps_reconciled_message():
    with (
        patch.object(
            mod, "_list_open_ledger_rows", AsyncMock(return_value=[object()])
        ),
        patch.object(
            mod,
            "_reconcile_one_ledger_row",
            AsyncMock(return_value={"verdict": "pending", "order_id": "A"}),
        ),
    ):
        out = await mod.kis_live_reconcile_orders_impl(dry_run=True)
    assert out["message"].startswith("Reconciled 1 live order(s)")
```

- [ ] **Run test to verify it fails**:

```bash
uv run pytest tests/mcp_server/test_kis_live_reconcile_window.py -v -k "message"
```

기대: `test_impl_empty_candidates_message_is_distinguishable` FAIL — 현재 메시지는 `"Reconciled 0 live order(s) (dry_run=True): {}"` 이므로 `assert "No open candidates ..." in out["message"]` 에서 `AssertionError`. nonempty 테스트는 PASS(행동 보존 고정).

- [ ] **Write minimal implementation** — `kis_live_ledger.py` `kis_live_reconcile_orders_impl` 반환부 — 현재 코드 (lines 612-621):

```python
    return {
        "success": True,
        "account_mode": "kis_live",
        "dry_run": dry_run,
        "counts": counts,
        "reconciled": reconciled,
        "message": (
            f"Reconciled {len(reconciled)} live order(s) (dry_run={dry_run}): {counts}"
        ),
    }
```

변경 후:

```python
    if rows:
        message = (
            f"Reconciled {len(reconciled)} live order(s) (dry_run={dry_run}): {counts}"
        )
    else:
        # ROB-487 UX: 후보 0건(모든 ledger 행이 terminal)을 누락과 구분해 표기.
        message = (
            "No open candidates (all ledger rows terminal) — nothing to reconcile "
            f"(dry_run={dry_run})"
        )
    return {
        "success": True,
        "account_mode": "kis_live",
        "dry_run": dry_run,
        "counts": counts,
        "reconciled": reconciled,
        "message": message,
    }
```

- [ ] **Run test to verify it passes**:

```bash
uv run pytest tests/mcp_server/test_kis_live_reconcile_window.py \
  tests/mcp_server/tooling/test_kis_live_ledger.py -v
```

기대: 전건 PASS (`test_reconcile_orders_impl_aggregates_counts`는 rows 비어있지 않은 경로라 메시지 불변).

- [ ] **Commit**:

```bash
uv run ruff format app/ tests/ && uv run ruff check app/ tests/
git add -A && git commit -m "$(cat <<'EOF'
fix(ROB-487): distinguish 'no open candidates' from 'Reconciled 0'

The 6/10 09:03 'Reconciled 0 live order(s)' read like a miss when it
actually meant every ledger row was already terminal.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 6: `_expected_krx_expiry` → `_expected_day_order_expiry` (NXT 마감 20:00 KST)

15:31:25 KST에 접수된 KAI 주문이 이미 과거인 `expected_expiry`(당일 15:30)를 받는 모순 해소. 사용처는 `kis_live_ledger.py:277` 한 곳뿐임을 grep으로 확인한다 (라우팅 노트 `:273-276`의 "SOR auto-route (KRX; NXT-eligible)"와 의미 정합).

**Files:**
- Modify: `app/mcp_server/tooling/kis_live_ledger.py` (:167-175 함수, :277 사용처)
- Test: `tests/mcp_server/test_kis_live_place_order_routing_surface.py` (:8-18, :62)

### Steps

- [ ] **사용처 전수 확인** (수정 전 실행, 결과를 PR 본문에 기록):

```bash
grep -rn "_expected_krx_expiry\|expected_expiry" app/ tests/ docs/runbooks/
```

기대: app/ 내 사용처는 `kis_live_ledger.py`의 정의(:167)와 `_record_kis_live_order` 내 사용(:277) 두 곳뿐. tests/는 `test_kis_live_place_order_routing_surface.py`, docs/는 런북 1곳(Task 7에서 갱신).

- [ ] **Write the failing test** — `tests/mcp_server/test_kis_live_place_order_routing_surface.py` 수정.

import + 테스트 — 현재 코드 (lines 8-18):

```python
from app.mcp_server.tooling.kis_live_ledger import (
    _expected_krx_expiry,
    _extract_broker_exchange,
)

KST = datetime.timezone(datetime.timedelta(hours=9))


def test_expected_krx_expiry_is_1530_kst_of_send_date():
    now = datetime.datetime(2026, 6, 9, 9, 43, tzinfo=KST)
    assert _expected_krx_expiry(now) == "2026-06-09T15:30:00+09:00"
```

변경 후:

```python
from app.mcp_server.tooling.kis_live_ledger import (
    _expected_day_order_expiry,
    _extract_broker_exchange,
)

KST = datetime.timezone(datetime.timedelta(hours=9))


def test_expected_day_order_expiry_is_2000_kst_of_send_date():
    # ROB-487: SOR day order는 NXT 마감(20:00 KST)까지 유효 — 15:31 NXT 세션
    # 주문이 과거 시각(15:30)의 expected_expiry를 받던 모순 해소.
    now = datetime.datetime(2026, 6, 9, 15, 31, 25, tzinfo=KST)
    assert _expected_day_order_expiry(now) == "2026-06-09T20:00:00+09:00"
```

마지막 assert — 현재 코드 (line 62):

```python
    assert resp["expected_expiry"].endswith("15:30:00+09:00")
```

변경 후:

```python
    assert resp["expected_expiry"].endswith("20:00:00+09:00")
```

- [ ] **Run test to verify it fails**:

```bash
uv run pytest tests/mcp_server/test_kis_live_place_order_routing_surface.py -v
```

기대: `ImportError: cannot import name '_expected_day_order_expiry' from 'app.mcp_server.tooling.kis_live_ledger'` (collection error).

- [ ] **Write minimal implementation** — `kis_live_ledger.py`.

함수 — 현재 코드 (lines 167-175):

```python
def _expected_krx_expiry(now: datetime.datetime) -> str | None:
    """KRX day-order expiry = 15:30 KST of the send date (ISO 8601), or None."""
    try:
        kst = datetime.timezone(datetime.timedelta(hours=9))
        local = now.astimezone(kst)
        close = local.replace(hour=15, minute=30, second=0, microsecond=0)
        return close.isoformat()
    except (ValueError, OverflowError):
        return None
```

변경 후 (KST는 Task 1에서 import됨):

```python
def _expected_day_order_expiry(now: datetime.datetime) -> str | None:
    """Day-order expiry = NXT close 20:00 KST of the send date (ISO 8601), or None.

    ROB-487: SOR day orders stay alive in the NXT session until 20:00 KST. The
    old KRX 15:30 stamp gave a 15:31 NXT-session order an expected_expiry that
    was already in the past at send time.
    """
    try:
        local = now.astimezone(KST)
        close = local.replace(hour=20, minute=0, second=0, microsecond=0)
        return close.isoformat()
    except (ValueError, OverflowError):
        return None
```

사용처 — 현재 코드 (line 277):

```python
        "expected_expiry": _expected_krx_expiry(now_kst()),
```

변경 후:

```python
        "expected_expiry": _expected_day_order_expiry(now_kst()),
```

- [ ] **Run test to verify it passes**:

```bash
uv run pytest tests/mcp_server/test_kis_live_place_order_routing_surface.py -v
grep -rn "_expected_krx_expiry" app/ tests/
```

기대: 테스트 전건 PASS, grep 0건 (잔존 참조 없음).

- [ ] **Commit**:

```bash
uv run ruff format app/ tests/ && uv run ruff check app/ tests/
git add -A && git commit -m "$(cat <<'EOF'
fix(ROB-487): expected_expiry stamps NXT close 20:00 KST

A 15:31:25 KST NXT-session order used to receive an expected_expiry of
15:30 the same day — already in the past at send time.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 7: 문서 정정 — `domestic_orders.py` docstring / 런북 / 도구 description

코드 변경 없음(문서·문자열만). 테스트 없음 — Task 8 풀 게이트에서 lint/format으로 검증.

**Files:**
- Modify: `app/services/brokers/kis/domestic_orders.py` (:540-542 docstring "Returns:" 블록 — #1223 이후 형태)
- Modify: `docs/runbooks/kis-live-order-reconcile.md` (:25-49)
- Modify: `app/mcp_server/tooling/orders_kis_variants.py` (:570-572 도구 description)

### Steps

- [ ] **`domestic_orders.py` docstring 정정** — `inquire_daily_order_domestic` docstring의 "Returns:" 블록. **주의(2026-06-10 재앵커)**: PR #1223(48b0d738, ROB-478~481)이 구 docstring의 가공 필드 리스트(`ord_no`/`rvse_cncl_dvsn_*` 등)를 이미 삭제하고 `max_pages` 파라미터를 추가했다. 따라서 이 스텝은 "허위 리스트 삭제"가 아니라 **실측 키 리스트 추가**다 — 현재 코드 (lines 540-542):

```
        Returns:
            체결 주문 목록 (list of dict)
        """
```

변경 후 (실측 키 리스트 — 2026-06-10 라이브 read-only 프로브):

```
        Returns:
            체결 주문 목록 (list of dict)

            실 응답 키 (TTTC8001R, 2026-06-10 read-only 라이브 프로브로 확정):
            - odno: 주문번호
            - orgn_odno: 원주문번호 (정정/취소 확인 행이 원주문을 가리킴)
            - sll_buy_dvsn_cd / sll_buy_dvsn_cd_name: 매도매수구분 (취소 확인
              행은 '매수취소'/'매도취소' 형태의 이름을 가짐)
            - pdno / prdt_name: 종목코드 / 상품명
            - ord_qty / ord_unpr: 주문수량 / 주문단가
            - tot_ccld_qty / tot_ccld_amt: 총체결수량 / 총체결금액
            - rmn_qty: 잔여수량 (> 0 이면 주문 생존)
            - rjct_qty: 거부수량 (미체결 day order는 EOD에 rjct_qty == ord_qty)
            - cncl_yn: 취소 여부 플래그
            - excg_id_dvsn_cd: 거래소 구분 (SOR/KRX/...)
            - ord_dt / ord_tmd: 주문일자 / 주문시각
            (체결가격은 fill_evidence가 avg_prvs → ccld_unpr → tot_ccld_amt
            순으로 해석한다.)

            주의 (ROB-487 실측):
            - prcs_stat_name / rvse_cncl_dvsn_cd / rvse_cncl_dvsn_name 키는
              실 응답에 존재하지 않는다.
            - 조회 윈도우(INQR_STRT_DT~INQR_END_DT)는 **주문일** 기준이다 —
              전일 주문/체결은 전일을 포함한 윈도우로만 보인다.
            - 페이지네이션이 동일 행을 중복 반환할 수 있다(실측: 모든 행 2회)
              — 소비자는 dedupe 필요 (fill_evidence._dedupe_rows 참고).
        """
```

- [ ] **런북 갱신** — `docs/runbooks/kis-live-order-reconcile.md`.

(1) "Reconcile workflow" 절(:25-32) 끝에 항목 추가 — 현재 3번 항목 다음에:

```markdown
4. 후보가 0건(모든 ledger 행이 terminal)이면 `"No open candidates (all ledger
   rows terminal)"` 메시지를 반환한다 — `Reconciled 0`이 누락으로 오인되지 않게
   구분됨 (ROB-487 UX). 증거 조회 윈도우는 각 주문의 **주문일~오늘**이라 익일
   reconcile도 전일 체결을 book할 수 있다.
```

(2) "Verdicts" 절 — 현재 코드 (lines 36-38):

```markdown
- `pending` — accepted, no fill yet; no-op (re-run later).
- `cancelled` — no daily-execution row; ledger marked cancelled; no journal side-effect.
- `expired` — KRX 마감을 지난 미체결 day order. reconcile이 `status="expired"`로 해소(영구 pending 방지). **Fail-closed**: 브로커가 주문을 live(접수/정상)로 보고하면 `expired`로 넘기지 않고 `pending` 유지(SOR 주문이 NXT 세션에서 살아있을 수 있음). 정확한 KIS 상태 문자열은 operator read-only smoke로 확정.
```

변경 후:

```markdown
- `pending` — accepted, no fill yet; no-op (re-run later). NXT 마감(20:00 KST)
  전의 미체결 day order는 항상 pending 유지 (ROB-487).
- `cancelled` — 취소 증거(`cncl_yn` truthy, 또는 `orgn_odno`로 매칭되는
  '매수취소'/'매도취소' 확인 행), 또는 주문일을 커버한 윈도우에서 일별체결 행
  부재. ledger만 마킹, journal side-effect 없음. 윈도우가 주문일 커버를 증명
  못 하면 noop (`noop_window_uncovered`).
- `expired` — **NXT 마감(20:00 KST) 이후** + 브로커 증거 `rjct_qty == ord_qty
  > 0` 인 미체결 day order (ROB-487 실측: 미체결 SOR day order는 EOD에
  rjct_qty가 전량으로 채워짐). **Fail-closed**: 둘 중 하나라도 없으면 `pending`
  유지(`rmn_qty > 0`이면 주문 생존). 실 TTTC8001R 행에는 `prcs_stat_name` /
  `rvse_cncl_dvsn_*` 키가 존재하지 않으므로(2026-06-10 라이브 프로브) 구
  상태-토큰 분류는 폐기됨.
```

(3) "Routing / lifecycle visibility" 절 — 현재 코드 (line 46):

```markdown
- `expected_expiry`: 주문일 KRX 마감(15:30 KST) ISO 시각.
```

변경 후:

```markdown
- `expected_expiry`: 주문일 NXT 마감(20:00 KST) ISO 시각 (ROB-487 — SOR day
  order는 NXT 세션까지 유효).
```

(4) NXT carry-over 블록쿼트 — 현재 코드 (line 49):

```markdown
> **NXT 세션 이월**: SOR-routed day order가 KRX 마감 후 NXT에서 살아있는지는 KIS 동작에 의존하며 **operator 확정 필요**(미상). 그래서 만료 해소는 fail-closed. ROB-463(NXT venue 파라미터 추가)과 보완관계.
```

변경 후:

```markdown
> **NXT 세션 이월 (ROB-487 실측 확정)**: SOR-routed day order는 KRX 마감 후에도
> NXT 세션(~20:00 KST)에서 체결될 수 있다 — 2026-06-09 KAI(047810) 15:31 주문이
> NXT 야간 체결로 booking됨. 미체결 SOR day order는 EOD에 `rjct_qty == ord_qty`
> (`tot_ccld_qty=0`)로 나타나고, 체결은 **주문일 윈도우**에서만 `tot_ccld_qty >
> 0`으로 나타난다(TTTC8001R은 주문일 기준 윈도우). 만료 해소는 여전히
> fail-closed(브로커 증거 + 20:00 시간 가드 둘 다 필요). ROB-463(NXT venue
> 파라미터 추가)과 보완관계.
```

- [ ] **도구 description 갱신** — `app/mcp_server/tooling/orders_kis_variants.py` — 현재 코드 (lines 570-572):

```python
            "Stale unfilled day orders are resolved to 'expired' once the "
            "KRX session has closed (fail-closed: a live broker status keeps "
            "them pending in case of NXT carryover). "
```

변경 후:

```python
            "Stale unfilled day orders are resolved to 'expired' only after "
            "NXT close (20:00 KST) AND broker evidence (rjct_qty == ord_qty); "
            "cancel-confirm rows resolve to 'cancelled'. Evidence is queried "
            "from each order's send date through today, so next-day "
            "reconciles still book prior-day fills. "
```

- [ ] **회귀 확인** (description 문자열을 단언하는 테스트가 없는지):

```bash
uv run pytest tests/mcp_server/tooling/test_kis_live_reconcile_tool.py -v
grep -rn "KRX session has closed" app/ tests/
```

기대: 테스트 PASS, grep 0건.

- [ ] **Commit**:

```bash
uv run ruff format app/ tests/ && uv run ruff check app/ tests/
git add -A && git commit -m "$(cat <<'EOF'
docs(ROB-487): correct TTTC8001R row-shape docs and NXT carry-over runbook

domestic_orders docstring now lists the live-verified keys (no
prcs_stat_name / rvse_cncl_dvsn_*), notes the order-date window and the
pagination duplication; runbook NXT carry-over is now empirically
characterized; reconcile tool description matches the new expiry rules.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 8: 풀 게이트 + PR 생성 + operator 검증 절차

**Files:** 없음 (검증/배포 절차)

### Steps

- [ ] **풀 게이트 실행**:

```bash
cd /Users/mgh3326/work/auto_trader.rob-487
make lint          # ruff check app/ tests/ + ruff format --check app/ tests/ + ty check app/ --error-on-warning
uv run pytest tests/services/brokers/kis/test_live_order_expiry.py \
  tests/mcp_server/test_kis_live_reconcile_window.py \
  tests/mcp_server/test_kis_live_reconcile_expiry.py \
  tests/mcp_server/test_kis_live_place_order_routing_surface.py \
  tests/mcp_server/tooling/test_kis_live_ledger.py \
  tests/mcp_server/tooling/test_kis_live_reconcile_tool.py \
  tests/tasks/test_kis_live_reconcile_tasks.py \
  tests/scripts/test_kis_live_auto_reconcile_cli.py \
  tests/brokers/kis/mock_scalping_exec/test_fill_evidence.py -v
```

기대: lint clean + 전건 PASS. (`test_kis_live_ledger.py`는 DB 필요 — `docker compose up -d` 선행. fill_evidence 테스트는 무변경 회귀 확인용.)

- [ ] **전체 스위트** (시간 여유 시 권장; CI가 어차피 전체 실행):

```bash
uv run pytest tests/ -m "not integration and not slow" -q
```

- [ ] **마이그레이션 0 확인** (merge-base 기준 — upstream에 migration이 머지돼도 false-positive 없음):

```bash
git diff "$(git merge-base HEAD origin/main)" --stat -- alembic/
```

기대: 출력 없음 (이 PR은 코드-only, migration 0).

- [ ] **최신 main 반영 후 재검증** (PR 직전 — upstream 드리프트로 인한 충돌/계약 변화 조기 발견):

```bash
git fetch --prune origin
git merge origin/main   # 충돌 시 해소 후 커밋
uv run pytest tests/services/brokers/kis/test_live_order_expiry.py tests/mcp_server/test_kis_live_reconcile_window.py tests/mcp_server/test_kis_live_reconcile_expiry.py -q
```

- [ ] **푸시 + PR 생성** (base `main`, 단일 PR). 주의: `--body "$(cat <<'EOF' ...)"`는 명령 치환을 감싼 큰따옴표가 필수다 — 불안해 보이면 본문을 임시 파일로 빼서 `--body-file`을 써도 된다:

```bash
git push -u origin rob-487
gh pr create --base main \
  --title "fix(ROB-487): reconcile 증거 윈도우 주문일 확장 + 실 TTTC8001R 기반 NXT-aware expiry" \
  --body "$(cat <<'EOF'
## 요약

**이슈 전제 정정**: 보고된 증상("6/10 아침 reconcile이 pending 주문을 누락")은 버그가 아님 — 6/9 19:02 KST의 dry_run=False reconcile이 KAI(047810) NXT 야간 체결을 정상 book(ledger 19→filled, trade 107, journal 64)하고 미체결 7건을 expired 처리했으며, 전부 라이브 TTTC8001R 프로브로 브로커 증거와 일치함을 확인. **데이터 복구 없음.** 본 PR은 검증에서 확인된 잠재 결함 2건 + UX + 문서 정정.

## 변경

- **증거 윈도우 확장**: `_fetch_live_daily_rows`가 오늘-only → 주문일(created_at KST)~오늘. TTTC8001R은 주문일 기준 윈도우(라이브 프로브: 20260610 윈도우에 6/9 주문 0건)라 기존 코드는 익일 reconcile에서 전일 체결을 절대 볼 수 없었음.
- **FillVerdict.NONE fail-closed**: 존재하지 않는 pending-orders 교차확인을 주장하던 거짓 주석 삭제; 윈도우가 주문일을 커버했음이 증명될 때만 cancelled 마킹, 아니면 `noop_window_uncovered` + reason.
- **expiry 분류기 재작성**: 실 TTTC8001R 행에는 `prcs_stat_name`/`rvse_cncl_dvsn_*` 키가 없어(라이브 확정) ROB-476 토큰 가드는 dead code였음. 신규: `cncl_yn`/`orgn_odno` 취소확인 → cancelled; `rjct_qty==ord_qty>0` AND NXT 마감(20:00 KST) 이후 → expired; 그 외 fail-closed pending. 순수 time-guard 폐기.
- **NXT-aware 시간 가드**: `nxt_session_closed` 순수 술어 신설. `market_session.kr_market_data_state`는 무변경(ROB-464 소비자 보호) — reconcile 경로에서 import만 제거.
- **UX**: 후보 0건 시 "No open candidates (all ledger rows terminal)" 메시지.
- **expected_expiry**: 15:30 → NXT 마감 20:00 KST (15:31 주문이 과거 expected_expiry를 받던 모순 해소).
- **문서**: domestic_orders docstring 실측 키 리스트, 런북 NXT carry-over 실측 확정, 도구 description.

## 불변식

- ROB-395 accepted-only/evidence-gated 유지: terminal 행 자동 re-open 없음, 후보 쿼리 무변경.
- migration 0.

## 테스트

- 분류기: 실 row 형태 fixture 13종(미체결 SOR pending / rjct 전량+20:00 후 expired / 20:00 전 fail-closed / 취소확인 orgn_odno / 페이지네이션 중복 멱등 / leading-zero 정규화).
- 커널: 익일 확장 윈도우 booking(KAI 실측 형태), 19:02 조기 expiry 회귀, 중복 행 이중계상 금지, 윈도우 미커버 시 terminal 마킹 거부, XKRX 분류기 제거 가드.

## Operator 검증 (읽기 전용, 머지 후)

PR 본문 하단 런북 절차 참고 (docs/runbooks/kis-live-order-reconcile.md).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **CI green 확인** 후 user 리뷰/머지 대기 (직접 머지 금지 — main 보호 브랜치).

- [ ] **Operator 검증 절차** (머지/배포 후, 읽기 전용 — Linear ROB-487 코멘트로 결과 기록):

  1. `kis_live_reconcile_orders(dry_run=True)` 호출 — 후보 0건이면 새 메시지 `"No open candidates (all ledger rows terminal)"` 확인.
  2. 미체결 day order가 있는 거래일 **15:31~19:59 KST**에 `dry_run=True` → 해당 행 `action == "noop_pending"` 확인 (`would_mark_expired`가 아니어야 함 — 6/9 19:02 조기 expiry 재발 방지 확인).
  3. 같은 주문을 **20:00 이후** `dry_run=True` → 브로커가 `rjct_qty == ord_qty`를 채웠다면 `would_mark_expired` 확인 → 이상 없으면 `dry_run=False` 적용.
  4. 전일 주문이 남은 익일 아침 `dry_run=True` → verdict가 무조건 `cancelled`가 아니라 브로커 증거 기반(`filled`/`expired`)인지 확인 (확장 윈도우 동작 검증).
  5. (잔여 불확실성 해소) NXT 장중(15:31~19:59)에 살아있는 미체결 주문 1건에 대해 read-only TTTC8001R 프로브로 `rjct_qty` 값이 0인지 1회 관찰 — 장중에 `rjct_qty`가 채워지는 사례가 발견되면 Linear에 기록 (현재 룰은 20:00 가드 뒤라 안전하지만 실측 보강).

---

## Follow-up (이 PR 스코프 밖 — 별도 이슈로)

- **TTTC8001R 페이지네이션 중복 조사**: 라이브 프로브 3개 윈도우(20260608/09/10) 모두에서 모든 행이 정확히 2회 반환됨(32행/16 unique). `inquire_daily_order_domestic`(domestic_orders.py:576-674)의 `CTX_AREA_NK100` 연속조회 처리 점검 필요. PR #1223이 `max_pages` 페이지 캡 + truncation `RuntimeError`를 추가했지만 중복 자체는 미해결. 현재는 `fill_evidence._dedupe_rows` + 신규 분류기의 any-row 술어로 무해하나, 다른 소비자가 합산하면 이중계상 위험.
- **6/9 19:02 dry_run=False 실행 주체 attribution**: 코드 결함 아님(동작은 정확했음). 필요 시 MCP 서버 로그/shell history 확인. ROB-475 TaskIQ는 config 기본값 False + .env 0 hits로 exoneration 확정.
- **mis-marked terminal 행 백필 도구**: 현재 데이터는 복구 불요(전수 브로커 증거 일치 확인). 향후 필요해지면 주기 커널이 아닌 **별도 명시적 operator 도구**로 (ROB-395 불변식 — 자동 re-open 금지).




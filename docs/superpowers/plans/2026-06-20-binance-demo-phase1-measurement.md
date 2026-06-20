# Binance Demo 스캘핑 Phase 1 — 측정 신뢰화 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 매일 모의 루프의 수치를 신뢰 가능하게 만든다 — (A) 라운드트립 net PnL을 ledger에 기록해 손실예산 게이트를 실효화하고, (B) 일별 buy&hold 벤치마크로 전략 vs 패시브를 비교한다.

**Architecture:** 기존 `demo_scalping_exec` 실행 경로 안의 두 변경. 변경 A는 `executor.py`가 close 행 reconcile 직전에 net PnL을 계산해 close 행 `extra_metadata['realized_pnl_usdt']`(부호 포함)에 durable하게 기록한다. 변경 B는 `scalping_daily_reviews`에 `benchmark_return_bps` 컬럼을 추가하고, market-data를 아는 **benchmark_runner**(+CLI)가 일별 buy&hold를 계산해 리뷰 행에 저장한다. 리뷰 라우터/서비스는 market-data를 import하지 않는다(ROB-315 경계).

**Tech Stack:** Python 3.13, SQLAlchemy(async) + asyncpg, Alembic, FastAPI, pytest-asyncio, Decimal.

> **스펙 정제 노트(§4.2 대비):** 스펙은 "ScalpingReviewService가 draft 시점에 벤치마크 계산"을 제안했으나, 라우터 정적 import-guard(`tests/test_invest_api_scalping_router.py:262`, 직접 import에 `brokers/binance/demo_scalping/market_data` 류 금지)와 서비스의 "broker 무접촉" 설계 원칙 때문에, 벤치마크의 market-data fetch는 **`demo_scalping_exec/benchmark_runner.py`(가드 밖, 이미 market-data-aware) + 운영 CLI**로 분리하고, 서비스에는 순수 저장 메서드(`set_benchmark`)만 추가한다. 측정 결과(컬럼·노출)는 스펙과 동일.

## Global Constraints

- Python 3.13+. 모든 변경은 worktree 브랜치 `feature/binance-demo-phase1-measurement`에서. canonical repo는 main 고정.
- Decimal은 문자열로 직렬화/저장(정밀도 보존). `realized_pnl_usdt`는 `str(Decimal)`로 ledger `extra_metadata`에 저장 — `ledger_state._realized_loss_today`가 `Decimal(str(raw))`로 복원(`ledger_state.py:41`).
- `realized_pnl_usdt`는 **부호 포함 net**(손실=음수). **close 행에만** 기록(open 행 미기록 → 라운드트립당 1회 집계, 이중계상 금지).
- `net_pnl_usdt`는 entry/exit fill price·qty·side·fee_rate에만 의존하고 `exit_reference_price`와 무관(`cost.py:176-184`) → close 행 기록값 == analytics 행 값(drift 없음).
- 마이그레이션은 additive nullable, 단일 head 유지. operator가 `alembic upgrade head` 수동 실행(프로덕션 cutover 게이트). 현재 head = `885e50ac5bb1`.
- 라우터 `app/routers/invest_scalping.py`에 금지 substring(`brokers`/`scheduler`/`executor`/`demo_scalping`/`binance`/`order_intent`/`upbit`/`alpaca`/`kis_trading`/`kis_holdings`) 포함 import를 추가하지 말 것(`tests/test_invest_api_scalping_router.py:247-281`).
- 스캘핑 리뷰 `account_scope`는 `"binance_demo"` 고정(`SCALPING_REVIEW_ACCOUNT_SCOPE`).
- 수수료 5bps 추정 + funding=0(`contract.py:35`, `cost.py:52`) → net은 낙관 편향(측정 caveat). "실제값" 단정 금지.
- TDD: 실패 테스트 → 최소 구현 → 통과 → 커밋. 베이스라인 515 green(데모 스캘핑 스코프).
- 커밋 trailer:
  ```
  Co-Authored-By: Paperclip <noreply@paperclip.ing>
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_0115yvcHYpLv3aLeKJ36Bo9w
  ```

---

### Task 1: 라운드트립 net PnL을 close 행 reconcile 메타데이터에 기록 (변경 A)

**Files:**
- Modify: `app/services/brokers/binance/demo_scalping_exec/executor.py` (helper 추가 + spot/futures close-row reconcile 2곳)
- Test: `tests/services/brokers/binance/demo_scalping_exec/test_executor_realized_pnl.py` (생성)

**Interfaces:**
- Consumes: `build_round_trip_economics(...)`, `DEMO_SCALPING_FEE_RATE_BPS`(둘 다 executor.py에 이미 import됨), `self._open_fill_price`, `self._close_fill_price`, `ledger.record_reconciled(*, client_order_id, now, extra_metadata_merge=None)`.
- Produces: close 행 `extra_metadata['realized_pnl_usdt']`(str(Decimal), 음수=손실) — `ledger_state._realized_loss_today`가 소비.

- [ ] **Step 1: 실패 테스트 작성**

`tests/services/brokers/binance/demo_scalping_exec/test_executor_realized_pnl.py` 생성. 먼저 fixture 블록을 **`tests/services/brokers/binance/demo_scalping_exec/test_executor_analytics.py`의 30-146행(`_NOW`, `_REF`, `_limits`, `_intent`, `_Ref`, `_MD`, `_Sub`, `_OO`, `_Pos`, `_FakeFutures`)을 그대로 복사**해 파일 상단에 둔다. 그 아래에:

```python
from sqlalchemy import select

from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService
from app.services.brokers.binance.demo_scalping.contract import (
    MarketConditions,
    ScalpingRiskLimits,
    evaluate_risk,
)
from app.services.brokers.binance.demo_scalping.ledger_state import load_ledger_snapshot


@pytest.mark.asyncio
async def test_close_row_carries_realized_pnl_open_row_does_not(db_session) -> None:
    client = _FakeFutures(open_px="100", close_px="100.40")
    md = _MD([100.0, 100.4])  # 2nd poll crosses TP
    ex = DemoScalpingExecutor(
        product="usdm_futures",
        client=client,
        session=db_session,
        reference=_Ref(),
        now=_NOW,
        limits=_limits("RPNLWINUSDT"),
        market_data=md,
        poll_delay_seconds=0.0,
    )
    result = await ex.execute_monitored(
        _intent("RPNLWINUSDT"), confirm=True, max_poll_count=5
    )
    assert result.status == "reconciled"

    analytics = await ScalpTradeAnalyticsService(db_session).get_by_open_client_order_id(
        result.open_client_order_id
    )
    close_row = await db_session.scalar(
        select(BinanceDemoOrderLedger).where(
            BinanceDemoOrderLedger.client_order_id == result.close_client_order_id
        )
    )
    open_row = await db_session.scalar(
        select(BinanceDemoOrderLedger).where(
            BinanceDemoOrderLedger.client_order_id == result.open_client_order_id
        )
    )
    # close row carries the signed net PnL, equal to the analytics net PnL.
    assert close_row.extra_metadata is not None
    assert "realized_pnl_usdt" in close_row.extra_metadata
    assert (
        Decimal(close_row.extra_metadata["realized_pnl_usdt"])
        == analytics.net_pnl_usdt
    )
    # open row is NOT stamped — single-count for _realized_loss_today.
    assert "realized_pnl_usdt" not in (open_row.extra_metadata or {})


@pytest.mark.asyncio
async def test_losing_round_trip_feeds_daily_loss_budget_gate(db_session) -> None:
    client = _FakeFutures(open_px="100", close_px="99")  # BUY then exit lower → loss
    ex = DemoScalpingExecutor(
        product="usdm_futures",
        client=client,
        session=db_session,
        reference=_Ref(),
        now=_NOW,
        limits=_limits("RPNLLOSSUSDT"),
        market_data=None,
        poll_delay_seconds=0.0,
    )
    result = await ex.execute(_intent("RPNLLOSSUSDT"), confirm=True)  # immediate open+close
    assert result.status == "reconciled"

    snapshot = await load_ledger_snapshot(
        BinanceDemoLedgerService(db_session),
        product="usdm_futures",
        symbol="RPNLLOSSUSDT",
        now=_NOW,
    )
    # gross loss ~ (99-100)*0.1 = -0.1 plus fees → realized loss >= 0.09.
    assert snapshot.realized_loss_today_usdt >= Decimal("0.09")

    decision = evaluate_risk(
        product="usdm_futures",
        symbol="RPNLLOSSUSDT",
        side="BUY",
        target_notional_usdt=Decimal("10"),
        limits=ScalpingRiskLimits(
            allowlist=frozenset({"RPNLLOSSUSDT"}),
            excluded=frozenset(),
            daily_loss_budget_usdt=Decimal("0.05"),
        ),
        ledger=snapshot,
        market=MarketConditions(
            spread_bps=Decimal("1"),
            data_age_seconds=1.0,
            spot_free_base_qty=Decimal("0"),
        ),
    )
    assert "daily_loss_budget_exhausted" in decision.reason_codes
```

- [ ] **Step 2: 실패 확인**

Run: `cd /Users/mgh3326/work/auto_trader.binance-phase1 && uv run pytest tests/services/brokers/binance/demo_scalping_exec/test_executor_realized_pnl.py -v`
Expected: FAIL — `test_close_row_carries_realized_pnl_open_row_does_not`에서 `"realized_pnl_usdt" not in close_row.extra_metadata` (현재 미기록), `test_losing_round_trip...`에서 `realized_loss_today_usdt == 0`이라 `daily_loss_budget_exhausted` 미발화.

- [ ] **Step 3: 헬퍼 추가**

`executor.py`에서 `_finalize_analytics`(라인 230) 메서드 **바로 위**에 헬퍼를 추가:

```python
    def _round_trip_realized_pnl_usdt(
        self, intent: OrderIntent, ref: Any, qty: Decimal
    ) -> Decimal | None:
        """Round-trip net PnL (USDT, signed; a loss is negative) for the durable
        daily-loss-budget gate (``ledger_state._realized_loss_today``). ``None``
        when either leg lacks a proven fill price — never fabricated. Independent
        of the exit *reference* price (that only moves exit-slippage telemetry),
        so this equals the analytics row's ``net_pnl_usdt``."""
        entry_fill = self._open_fill_price
        if entry_fill is None or self._close_fill_price is None:
            return None
        econ = build_round_trip_economics(
            side=intent.side,
            qty=qty,
            entry_reference_price=intent.entry_reference_price or ref.price,
            entry_fill_price=entry_fill,
            fee_rate_bps=DEMO_SCALPING_FEE_RATE_BPS,
            exit_fill_price=self._close_fill_price,
        )
        return econ.net_pnl_usdt
```

- [ ] **Step 4: spot close-row reconcile에 기록**

`executor.py`의 `_close_and_reconcile_spot` 라인 916-920을 교체:

```python
            if close_cid is not None and close_filled:
                realized_pnl = self._round_trip_realized_pnl_usdt(intent, ref, qty)
                await self.ledger.record_closed(client_order_id=close_cid, now=self.now)
                await self.ledger.record_reconciled(
                    client_order_id=close_cid,
                    now=self.now,
                    extra_metadata_merge=(
                        {"realized_pnl_usdt": str(realized_pnl)}
                        if realized_pnl is not None
                        else None
                    ),
                )
```

- [ ] **Step 5: futures close-row reconcile에 기록**

`executor.py`의 `_close_and_reconcile_futures` 라인 1035-1039를 교체:

```python
            if close_cid is not None and close_filled:
                realized_pnl = self._round_trip_realized_pnl_usdt(intent, ref, qty)
                await self.ledger.record_closed(client_order_id=close_cid, now=self.now)
                await self.ledger.record_reconciled(
                    client_order_id=close_cid,
                    now=self.now,
                    extra_metadata_merge=(
                        {"realized_pnl_usdt": str(realized_pnl)}
                        if realized_pnl is not None
                        else None
                    ),
                )
```

- [ ] **Step 6: 통과 확인 + 회귀**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_exec/test_executor_realized_pnl.py tests/services/brokers/binance/demo_scalping_exec/test_executor_analytics.py -v`
Expected: PASS (신규 2개 + 기존 analytics 회귀 전부 green — econ 값 불변, 위치만 추가).

- [ ] **Step 7: 커밋**

```bash
cd /Users/mgh3326/work/auto_trader.binance-phase1
git add app/services/brokers/binance/demo_scalping_exec/executor.py tests/services/brokers/binance/demo_scalping_exec/test_executor_realized_pnl.py
git commit -F - <<'EOF'
feat: record round-trip realized_pnl_usdt on close ledger row (변경 A)

executor가 close 행 reconcile 직전 net PnL을 계산해 close 행
extra_metadata['realized_pnl_usdt'](부호 포함)에 durable 기록 →
ledger_state._realized_loss_today / DAILY_LOSS_BUDGET_EXHAUSTED 게이트 실효화.
close 행에만 기록(이중계상 방지), partial 행은 미기록(날조 금지).

Co-Authored-By: Paperclip <noreply@paperclip.ing>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0115yvcHYpLv3aLeKJ36Bo9w
EOF
```

---

### Task 2: `benchmark_return_bps` 컬럼 + 마이그레이션 (변경 B-1)

**Files:**
- Modify: `app/models/scalping_reviews.py` (컬럼 추가, `net_return_bps` 다음)
- Create: `alembic/versions/20260620_scalp_benchmark_return_bps.py`
- Test: `tests/services/scalping_reviews/test_benchmark_column.py` (생성)

**Interfaces:**
- Produces: `ScalpingDailyReview.benchmark_return_bps: Mapped[Decimal | None]` (Numeric(12,4), nullable).

- [ ] **Step 1: 실패 테스트 작성**

`tests/services/scalping_reviews/test_benchmark_column.py` 생성:

```python
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.models.scalping_reviews import ScalpingDailyReview

_NOW = dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.UTC)


@pytest.mark.asyncio
async def test_benchmark_return_bps_column_round_trips(db_session) -> None:
    review = ScalpingDailyReview(
        review_date=dt.date(2026, 6, 20),
        product="usdm_futures",
        account_scope="binance_demo",
        session_tag="",
        decision="review",
        status="draft",
        created_at=_NOW,
        updated_at=_NOW,
        benchmark_return_bps=Decimal("12.3456"),
    )
    db_session.add(review)
    await db_session.flush()
    await db_session.refresh(review)
    assert review.benchmark_return_bps == Decimal("12.3456")
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/scalping_reviews/test_benchmark_column.py -v`
Expected: FAIL — `TypeError: 'benchmark_return_bps' is an invalid keyword argument` (모델에 컬럼 없음).

- [ ] **Step 3: 모델에 컬럼 추가**

`app/models/scalping_reviews.py`의 `net_return_bps` 컬럼(약 122-124행) **바로 다음**에 추가:

```python
    benchmark_return_bps: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
```

- [ ] **Step 4: 마이그레이션 생성**

`alembic/versions/20260620_scalp_benchmark_return_bps.py` 생성:

```python
"""add benchmark_return_bps to scalping_daily_reviews

Revision ID: 20260620_scalp_benchmark
Revises: 885e50ac5bb1
Create Date: 2026-06-20

Phase 1 — daily buy&hold benchmark column for the demo scalping review.
Additive nullable; safe forward/backward.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260620_scalp_benchmark"
down_revision: str | Sequence[str] | None = "885e50ac5bb1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scalping_daily_reviews",
        sa.Column("benchmark_return_bps", sa.Numeric(12, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scalping_daily_reviews", "benchmark_return_bps")
```

- [ ] **Step 5: 단일 head + 마이그레이션 적용 확인**

Run: `uv run alembic heads`
Expected: 단일 head `20260620_scalp_benchmark (head)`.
Run: `uv run alembic upgrade head && uv run alembic current`
Expected: 에러 없이 `20260620_scalp_benchmark`.

- [ ] **Step 6: 통과 확인**

Run: `uv run pytest tests/services/scalping_reviews/test_benchmark_column.py -v`
Expected: PASS.

- [ ] **Step 7: 커밋**

```bash
git add app/models/scalping_reviews.py alembic/versions/20260620_scalp_benchmark_return_bps.py tests/services/scalping_reviews/test_benchmark_column.py
git commit -F - <<'EOF'
feat: add benchmark_return_bps to scalping_daily_reviews (변경 B-1)

일별 buy&hold 벤치마크 저장용 additive nullable 컬럼 + 마이그레이션
(down_revision 885e50ac5bb1, 단일 head). operator alembic upgrade 게이트.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0115yvcHYpLv3aLeKJ36Bo9w
EOF
```

---

### Task 3: 순수 벤치마크 함수 (변경 B-2)

**Files:**
- Create: `app/services/scalping_reviews/benchmark.py`
- Test: `tests/services/scalping_reviews/test_benchmark.py` (생성)

**Interfaces:**
- Produces:
  - `daily_buy_and_hold_return_bps(*, open_price: Decimal, close_price: Decimal) -> Decimal`
  - `notional_weighted_benchmark_bps(weighted: Sequence[tuple[Decimal, Decimal]]) -> Decimal | None` (각 원소 `(notional_usdt, benchmark_bps)`)

- [ ] **Step 1: 실패 테스트 작성**

`tests/services/scalping_reviews/test_benchmark.py` 생성:

```python
from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.scalping_reviews.benchmark import (
    daily_buy_and_hold_return_bps,
    notional_weighted_benchmark_bps,
)


def test_daily_buy_and_hold_return_bps_up_down_flat() -> None:
    assert daily_buy_and_hold_return_bps(
        open_price=Decimal("100"), close_price=Decimal("101")
    ) == Decimal("100")
    assert daily_buy_and_hold_return_bps(
        open_price=Decimal("100"), close_price=Decimal("99")
    ) == Decimal("-100")
    assert daily_buy_and_hold_return_bps(
        open_price=Decimal("100"), close_price=Decimal("100")
    ) == Decimal("0")


def test_daily_buy_and_hold_rejects_nonpositive_open() -> None:
    with pytest.raises(ValueError):
        daily_buy_and_hold_return_bps(open_price=Decimal("0"), close_price=Decimal("1"))


def test_notional_weighted_benchmark_bps_weights_by_notional() -> None:
    # (100*100 + 300*-20) / 400 = 10
    assert notional_weighted_benchmark_bps(
        [(Decimal("100"), Decimal("100")), (Decimal("300"), Decimal("-20"))]
    ) == Decimal("10")


def test_notional_weighted_benchmark_bps_none_when_no_notional() -> None:
    assert notional_weighted_benchmark_bps([]) is None
    assert notional_weighted_benchmark_bps([(Decimal("0"), Decimal("100"))]) is None
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/scalping_reviews/test_benchmark.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.scalping_reviews.benchmark`.

- [ ] **Step 3: 모듈 구현**

`app/services/scalping_reviews/benchmark.py` 생성:

```python
"""Phase 1 — pure daily buy&hold benchmark math for the demo scalping review.

No DB, no network, no market-data client. The market-data fetch + storage
live in ``demo_scalping_exec.benchmark_runner``; this module stays pure so it
is trivially testable and safe to import from the review service boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

_BPS = Decimal("10000")


def daily_buy_and_hold_return_bps(
    *, open_price: Decimal, close_price: Decimal
) -> Decimal:
    """Passive buy&hold return over the day in bps: ``(close/open - 1) * 1e4``."""
    if open_price <= 0:
        raise ValueError(f"open_price must be > 0, got {open_price}")
    return (close_price / open_price - Decimal("1")) * _BPS


def notional_weighted_benchmark_bps(
    weighted: Sequence[tuple[Decimal, Decimal]],
) -> Decimal | None:
    """Notional-weighted mean of per-symbol benchmark bps.

    ``weighted`` is a sequence of ``(notional_usdt, benchmark_bps)`` pairs.
    Mirrors the strategy ``net_return_bps`` capital-weighting (rollup.py) so
    strategy vs benchmark are comparable. Returns ``None`` when there is no
    positive notional to weight by."""
    total_notional = sum((n for n, _ in weighted), Decimal("0"))
    if total_notional <= 0:
        return None
    weighted_sum = sum((n * b for n, b in weighted), Decimal("0"))
    return weighted_sum / total_notional
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/services/scalping_reviews/test_benchmark.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: 커밋**

```bash
git add app/services/scalping_reviews/benchmark.py tests/services/scalping_reviews/test_benchmark.py
git commit -F - <<'EOF'
feat: pure daily buy&hold benchmark math (변경 B-2)

daily_buy_and_hold_return_bps + notional_weighted_benchmark_bps (순수, DB/네트워크
없음). 전략 net_return_bps와 동일한 capital-weighting.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0115yvcHYpLv3aLeKJ36Bo9w
EOF
```

---

### Task 4: 서비스 저장 메서드 + 직렬화 노출 (변경 B-3)

**Files:**
- Modify: `app/services/scalping_reviews/service.py` (`set_benchmark` 추가)
- Modify: `app/routers/invest_scalping.py` (`_serialize_review` metrics에 `benchmarkReturnBps`)
- Test: `tests/services/scalping_reviews/test_service.py` (append) + `tests/services/scalping_reviews/test_serialize_benchmark.py` (생성)

**Interfaces:**
- Consumes: `ScalpingDailyReview.benchmark_return_bps`(Task 2), `_get_by_key`, `SCALPING_REVIEW_ACCOUNT_SCOPE`, `_require_demo_scope`.
- Produces: `ScalpingReviewService.set_benchmark(*, review_date, product, value, now, session_tag="", account_scope=SCALPING_REVIEW_ACCOUNT_SCOPE, detail=None) -> ScalpingDailyReview | None` — benchmark_runner(Task 5)가 소비.

- [ ] **Step 1: 실패 테스트 작성 (서비스)**

`tests/services/scalping_reviews/test_service.py` **맨 끝에 append** (기존 `_instrument`/`_analytics`/`_DATE`/`_NOW` 헬퍼 재사용):

```python
@pytest.mark.asyncio
async def test_set_benchmark_persists_value_and_detail(db_session) -> None:
    iid = await _instrument(db_session)
    await _analytics(
        db_session, iid, tag="w",
        entry_price=Decimal("100"), exit_price=Decimal("101"),
        entry_notional_usdt=Decimal("100"), net_pnl_usdt=Decimal("0.9"),
        gross_pnl_usdt=Decimal("1.0"), exit_reason="take_profit",
    )
    svc = ScalpingReviewService(db_session)
    await svc.build_draft(review_date=_DATE, product="usdm_futures", now=_NOW)
    updated = await svc.set_benchmark(
        review_date=_DATE, product="usdm_futures", value=Decimal("12.5"), now=_NOW,
        detail={"XRPUSDT": {"open": "100", "close": "100.5", "bps": "50"}},
    )
    assert updated is not None
    assert updated.benchmark_return_bps == Decimal("12.5")
    assert updated.source_payload["benchmark"]["XRPUSDT"]["bps"] == "50"
    assert updated.net_pnl_usdt == Decimal("0.9")  # rollup metrics preserved


@pytest.mark.asyncio
async def test_set_benchmark_noop_on_missing_review(db_session) -> None:
    svc = ScalpingReviewService(db_session)
    assert await svc.set_benchmark(
        review_date=dt.date(2099, 1, 1), product="usdm_futures",
        value=Decimal("1"), now=_NOW,
    ) is None


@pytest.mark.asyncio
async def test_set_benchmark_skips_locked_review(db_session) -> None:
    iid = await _instrument(db_session)
    await _analytics(
        db_session, iid, tag="a",
        entry_price=Decimal("100"), exit_price=Decimal("101"),
        entry_notional_usdt=Decimal("100"), net_pnl_usdt=Decimal("0.5"),
        gross_pnl_usdt=Decimal("0.6"), exit_reason="take_profit",
    )
    svc = ScalpingReviewService(db_session)
    r = await svc.build_draft(review_date=_DATE, product="usdm_futures", now=_NOW)
    await svc.update_review(r.id, now=_NOW, status="locked")
    res = await svc.set_benchmark(
        review_date=_DATE, product="usdm_futures", value=Decimal("9"), now=_NOW
    )
    assert res is not None and res.status == "locked"
    assert res.benchmark_return_bps is None  # not written while locked
```

`tests/services/scalping_reviews/test_serialize_benchmark.py` 생성:

```python
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.models.scalping_reviews import ScalpingDailyReview
from app.routers.invest_scalping import _serialize_review

_NOW = dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.UTC)


def test_serialize_review_includes_benchmark_return_bps() -> None:
    review = ScalpingDailyReview(
        review_date=dt.date(2026, 6, 20),
        product="usdm_futures",
        account_scope="binance_demo",
        session_tag="",
        decision="review",
        status="draft",
        created_at=_NOW,
        updated_at=_NOW,
        benchmark_return_bps=Decimal("7.5"),
    )
    out = _serialize_review(review)
    assert out["metrics"]["benchmarkReturnBps"] == "7.5"
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/scalping_reviews/test_service.py -k set_benchmark tests/services/scalping_reviews/test_serialize_benchmark.py -v`
Expected: FAIL — `AttributeError: 'ScalpingReviewService' object has no attribute 'set_benchmark'` 및 `KeyError: 'benchmarkReturnBps'`.

- [ ] **Step 3: 서비스 메서드 추가**

`app/services/scalping_reviews/service.py`의 `_apply_rollup`(static, 라인 148-163) **바로 다음**에 추가:

```python
    async def set_benchmark(
        self,
        *,
        review_date: dt.date,
        product: str,
        value: Decimal | None,
        now: dt.datetime,
        session_tag: str = "",
        account_scope: str = SCALPING_REVIEW_ACCOUNT_SCOPE,
        detail: dict[str, Any] | None = None,
    ) -> ScalpingDailyReview | None:
        """Store the daily buy&hold benchmark on an existing review row.

        Separate from ``build_draft`` (rollup-only, never imports a market-data
        client) so the market-data-aware ``benchmark_runner`` computes the value
        out of band and persists it here. No-op on a missing row (``None``) or a
        ``locked`` review (returned untouched). ``detail`` (per-symbol audit) is
        merged under ``source_payload['benchmark']``."""
        _require_demo_scope(account_scope)
        review = await self._get_by_key(
            review_date, product, account_scope, session_tag
        )
        if review is None or review.status == "locked":
            return review
        review.benchmark_return_bps = value
        if detail is not None:
            review.source_payload = {
                **(review.source_payload or {}),
                "benchmark": detail,
            }
        review.updated_at = now
        await self._session.flush()
        return review
```

`Decimal`은 import 필요: `service.py` 상단 import에 `from decimal import Decimal` 추가(현재 없음). `Any`/`dt`는 이미 import됨.

- [ ] **Step 4: 라우터 직렬화 노출**

`app/routers/invest_scalping.py`의 `_serialize_review` metrics dict에서 `"netReturnBps": _num(review.net_return_bps),`(라인 87) **바로 다음**에 추가:

```python
            "benchmarkReturnBps": _num(review.benchmark_return_bps),
```

(import 추가 없음 — 라우터 가드 위반 없음.)

- [ ] **Step 5: 통과 확인 + 라우터 가드 회귀**

Run: `uv run pytest tests/services/scalping_reviews/test_service.py tests/services/scalping_reviews/test_serialize_benchmark.py tests/test_invest_api_scalping_router.py -v`
Expected: PASS (set_benchmark 3개 + serialize 1개 + 기존 라우터/서비스 회귀 전부 green; `test_router_imports_no_broker_order_scheduler_modules` 포함).

- [ ] **Step 6: 커밋**

```bash
git add app/services/scalping_reviews/service.py app/routers/invest_scalping.py tests/services/scalping_reviews/test_service.py tests/services/scalping_reviews/test_serialize_benchmark.py
git commit -F - <<'EOF'
feat: ScalpingReviewService.set_benchmark + serialize benchmarkReturnBps (변경 B-3)

순수 저장 메서드(locked skip, source_payload['benchmark'] 머지) + 라우터
직렬화 노출. 서비스/라우터는 market-data 무import(ROB-315 경계 유지).

Co-Authored-By: Paperclip <noreply@paperclip.ing>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0115yvcHYpLv3aLeKJ36Bo9w
EOF
```

---

### Task 5: 벤치마크 runner + 운영 CLI (변경 B-4)

**Files:**
- Create: `app/services/brokers/binance/demo_scalping_exec/benchmark_runner.py`
- Create: `scripts/binance_demo_scalping_benchmark.py`
- Test: `tests/services/brokers/binance/demo_scalping_exec/test_benchmark_runner.py` (생성)

**Interfaces:**
- Consumes: `market_data.fetch_klines(product, symbol, interval="1d", limit=1) -> list[Candle]`, `ScalpingReviewService.list_analytics`/`set_benchmark`(Task 4), 순수 벤치마크 함수(Task 3).
- Produces: `compute_and_store_daily_benchmark(*, session, market_data, review_date, product, now, session_tag="", account_scope=...) -> Decimal | None`.

- [ ] **Step 1: 실패 테스트 작성**

`tests/services/brokers/binance/demo_scalping_exec/test_benchmark_runner.py` 생성. `_instrument`/`_analytics`/`_DATE`/`_NOW` 헬퍼를 **`tests/services/scalping_reviews/test_service.py` 22-67행에서 복사**해 상단에 두고:

```python
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.crypto_instruments import CryptoInstrument
from app.models.scalp_trade_analytics import ScalpTradeAnalytics
from app.services.brokers.binance.demo_scalping.signal import Candle
from app.services.brokers.binance.demo_scalping_exec.benchmark_runner import (
    compute_and_store_daily_benchmark,
)
from app.services.scalping_reviews.service import ScalpingReviewService

# (여기에 test_service.py 22-67행의 _DATE / _NOW / _instrument / _analytics 복사)


class _FakeMD:
    """day candle per symbol: {symbol: (open, close)}."""

    def __init__(self, prices: dict[str, tuple[float, float]]) -> None:
        self._prices = prices
        self.calls: list[tuple] = []

    async def fetch_klines(self, product, symbol, *, interval="1m", limit=50):
        self.calls.append((product, symbol, interval, limit))
        o, c = self._prices[symbol]
        return [
            Candle(
                open_time_ms=0,
                open=Decimal(str(o)),
                high=Decimal(str(max(o, c))),
                low=Decimal(str(min(o, c))),
                close=Decimal(str(c)),
                close_time_ms=0,
            )
        ]


class _BoomMD:
    async def fetch_klines(self, *a, **k):
        raise RuntimeError("network down")


@pytest.mark.asyncio
async def test_runner_stores_notional_weighted_benchmark(db_session) -> None:
    iid_x = await _instrument(db_session, "XRPUSDT")
    iid_d = await _instrument(db_session, "DOGEUSDT")
    await _analytics(
        db_session, iid_x, tag="x", symbol="XRPUSDT",
        entry_price=Decimal("100"), exit_price=Decimal("101"),
        entry_notional_usdt=Decimal("100"), net_pnl_usdt=Decimal("0.9"),
        gross_pnl_usdt=Decimal("1.0"), exit_reason="take_profit",
    )
    await _analytics(
        db_session, iid_d, tag="d", symbol="DOGEUSDT",
        entry_price=Decimal("0.1"), exit_price=Decimal("0.1"),
        entry_notional_usdt=Decimal("300"), net_pnl_usdt=Decimal("0"),
        gross_pnl_usdt=Decimal("0"), exit_reason="timeout",
    )
    svc = ScalpingReviewService(db_session)
    await svc.build_draft(review_date=_DATE, product="usdm_futures", now=_NOW)
    md = _FakeMD({"XRPUSDT": (100.0, 101.0), "DOGEUSDT": (100.0, 99.8)})  # +100bps, -20bps
    value = await compute_and_store_daily_benchmark(
        session=db_session, market_data=md, review_date=_DATE,
        product="usdm_futures", now=_NOW,
    )
    # (100*100 + 300*-20) / 400 = 10
    assert value == Decimal("10")
    review = await svc._get_by_key(_DATE, "usdm_futures", "binance_demo", "")
    assert review.benchmark_return_bps == Decimal("10")
    assert "XRPUSDT" in review.source_payload["benchmark"]
    assert all(c[2] == "1d" and c[3] == 1 for c in md.calls)  # day candle only


@pytest.mark.asyncio
async def test_runner_returns_none_when_market_data_fails(db_session) -> None:
    iid = await _instrument(db_session, "XRPUSDT")
    await _analytics(
        db_session, iid, tag="x", symbol="XRPUSDT",
        entry_price=Decimal("100"), exit_price=Decimal("101"),
        entry_notional_usdt=Decimal("100"), net_pnl_usdt=Decimal("0.9"),
        gross_pnl_usdt=Decimal("1.0"), exit_reason="take_profit",
    )
    svc = ScalpingReviewService(db_session)
    await svc.build_draft(review_date=_DATE, product="usdm_futures", now=_NOW)
    value = await compute_and_store_daily_benchmark(
        session=db_session, market_data=_BoomMD(), review_date=_DATE,
        product="usdm_futures", now=_NOW,
    )
    assert value is None
    review = await svc._get_by_key(_DATE, "usdm_futures", "binance_demo", "")
    assert review.benchmark_return_bps is None
```

> 참고: `_instrument(session, symbol)`은 `test_service.py`에서 두 번째 인자가 기본값 `"RVWXRPUSDT"`인 위치 인자다 — 위 테스트는 `"XRPUSDT"`/`"DOGEUSDT"`를 명시 전달한다. `_analytics(..., symbol=...)`는 `**kw`로 symbol을 덮어쓴다.

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_exec/test_benchmark_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: ...demo_scalping_exec.benchmark_runner`.

- [ ] **Step 3: runner 구현**

`app/services/brokers/binance/demo_scalping_exec/benchmark_runner.py` 생성:

```python
"""Phase 1 — compute + store the daily buy&hold benchmark for a scalping review.

Bridges the Demo market-data client (day open/close klines) and the pure
benchmark math to the review service's storage. Lives under
``demo_scalping_exec`` (already market-data-aware) — NOT under the review
router/service, which must stay broker/market-data-free (ROB-315 boundary).
Best-effort: any market-data failure leaves the benchmark NULL (the review
still renders the strategy net PnL).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from decimal import Decimal
from typing import Any

from app.services.scalping_reviews.benchmark import (
    daily_buy_and_hold_return_bps,
    notional_weighted_benchmark_bps,
)
from app.services.scalping_reviews.service import (
    SCALPING_REVIEW_ACCOUNT_SCOPE,
    ScalpingReviewService,
)

logger = logging.getLogger(__name__)


async def compute_and_store_daily_benchmark(
    *,
    session: Any,
    market_data: Any,
    review_date: dt.date,
    product: str,
    now: dt.datetime,
    session_tag: str = "",
    account_scope: str = SCALPING_REVIEW_ACCOUNT_SCOPE,
) -> Decimal | None:
    """Compute the notional-weighted daily buy&hold benchmark from that day's
    fill-proven analytics rows and store it on the review row. Returns the
    stored value (``None`` when it cannot be computed). Never raises on a
    market-data failure — logs and stores ``None``."""
    service = ScalpingReviewService(session)
    rows = await service.list_analytics(review_date=review_date, product=product)

    notional_by_symbol: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in rows:
        if row.entry_price is None or row.entry_notional_usdt is None:
            continue  # partial/anomaly row — no capital basis
        notional_by_symbol[row.symbol] += row.entry_notional_usdt

    value: Decimal | None = None
    detail: dict[str, Any] = {}
    try:
        weighted: list[tuple[Decimal, Decimal]] = []
        for symbol, notional in notional_by_symbol.items():
            candles = await market_data.fetch_klines(
                product, symbol, interval="1d", limit=1
            )
            if not candles:
                continue
            candle = candles[0]
            bps = daily_buy_and_hold_return_bps(
                open_price=candle.open, close_price=candle.close
            )
            weighted.append((notional, bps))
            detail[symbol] = {
                "open": str(candle.open),
                "close": str(candle.close),
                "bps": str(bps),
                "notional_usdt": str(notional),
            }
        value = notional_weighted_benchmark_bps(weighted)
    except Exception:  # noqa: BLE001 — benchmark is best-effort, never fatal
        logger.exception(
            "daily benchmark computation failed for %s %s", product, review_date
        )
        return None

    await service.set_benchmark(
        review_date=review_date,
        product=product,
        value=value,
        now=now,
        session_tag=session_tag,
        account_scope=account_scope,
        detail=detail or None,
    )
    return value
```

- [ ] **Step 4: runner 테스트 통과 확인**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_exec/test_benchmark_runner.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: CLI 구현 + 파싱 테스트**

`scripts/binance_demo_scalping_benchmark.py` 생성:

```python
"""Phase 1 — operator CLI: build the daily scalping review draft + benchmark.

Builds (or refreshes) the ``scalping_daily_reviews`` draft for a day/product
from ``scalp_trade_analytics``, then computes + stores the notional-weighted
daily buy&hold benchmark (Demo public klines, read-only). No broker/order
mutation. Demo data hosts only.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sys

logger = logging.getLogger("scalping_benchmark")

_VALID_PRODUCTS = ("spot", "usdm_futures")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Build the daily demo scalping review draft + notional-weighted "
            "buy&hold benchmark (read-only; Demo data hosts only)."
        )
    )
    p.add_argument("--product", required=True, choices=_VALID_PRODUCTS)
    p.add_argument("--date", required=True, help="UTC review date YYYY-MM-DD")
    p.add_argument("--session-tag", default="")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    review_date = dt.date.fromisoformat(args.date)
    now = dt.datetime.now(dt.UTC)

    from app.core.db import AsyncSessionLocal
    from app.services.brokers.binance.demo_scalping.market_data import (
        DemoScalpingMarketData,
    )
    from app.services.brokers.binance.demo_scalping_exec.benchmark_runner import (
        compute_and_store_daily_benchmark,
    )
    from app.services.scalping_reviews.service import ScalpingReviewService

    market_data = DemoScalpingMarketData()
    try:
        async with AsyncSessionLocal() as session:
            service = ScalpingReviewService(session)
            review = await service.build_draft(
                review_date=review_date,
                product=args.product,
                now=now,
                session_tag=args.session_tag,
            )
            value = await compute_and_store_daily_benchmark(
                session=session,
                market_data=market_data,
                review_date=review_date,
                product=args.product,
                now=now,
                session_tag=args.session_tag,
            )
            await session.commit()
    finally:
        await market_data.aclose()

    print(
        json.dumps(
            {
                "event": "scalping_benchmark",
                "review_id": review.id,
                "review_date": args.date,
                "product": args.product,
                "net_return_bps": (
                    None if review.net_return_bps is None else str(review.net_return_bps)
                ),
                "benchmark_return_bps": None if value is None else str(value),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=args.log_level.upper())
    try:
        return asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001 — top-level CLI guard
        logger.error("scalping benchmark CLI failed: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
```

`tests/scripts/test_binance_demo_scalping_benchmark.py` 생성:

```python
from __future__ import annotations

from scripts.binance_demo_scalping_benchmark import _parse_args


def test_cli_parse_args() -> None:
    ns = _parse_args(["--product", "usdm_futures", "--date", "2026-06-20"])
    assert ns.product == "usdm_futures"
    assert ns.date == "2026-06-20"
    assert ns.session_tag == ""
```

- [ ] **Step 6: CLI 테스트 통과 확인**

Run: `uv run pytest tests/scripts/test_binance_demo_scalping_benchmark.py -v`
Expected: PASS.

- [ ] **Step 7: 커밋**

```bash
git add app/services/brokers/binance/demo_scalping_exec/benchmark_runner.py scripts/binance_demo_scalping_benchmark.py tests/services/brokers/binance/demo_scalping_exec/test_benchmark_runner.py tests/scripts/test_binance_demo_scalping_benchmark.py
git commit -F - <<'EOF'
feat: daily benchmark runner + operator CLI (변경 B-4)

demo_scalping_exec.benchmark_runner가 fill-proven analytics 행의 종목별 notional로
일별 buy&hold(1d klines)를 notional 가중 계산해 리뷰 행에 저장(best-effort, 실패시 NULL).
운영 CLI scripts/binance_demo_scalping_benchmark.py가 draft+benchmark를 묶어 실행.
라우터/서비스 경계 유지(market-data는 runner/CLI에만).

Co-Authored-By: Paperclip <noreply@paperclip.ing>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0115yvcHYpLv3aLeKJ36Bo9w
EOF
```

---

### 최종 검증 (전체 스위트)

- [ ] **Step: 데모 스캘핑 스코프 전체 회귀**

Run: `uv run pytest tests/ -q -k "demo_scalping or scalping or binance_demo or futures_demo" -p no:cacheprovider`
Expected: 베이스라인 515 + 신규 테스트 전부 PASS, 0 failures.

- [ ] **Step: lint/format/type**

Run: `uv run ruff format app/ scripts/ tests/ && uv run ruff check app/ scripts/ tests/ && uv run ty check app/`
Expected: clean (또는 변경 무관 기존 경고만).

---

## Self-Review (spec 대비)

- **변경 A (결함#1)** → Task 1. close 행에 realized_pnl_usdt durable 기록, 단일집계, partial 미기록, 게이트 발화 e2e. ✓
- **변경 B (벤치마크)** → Task 2(컬럼/마이그레이션) + Task 3(순수 계산) + Task 4(저장/노출) + Task 5(runner/CLI). 일별 buy&hold, notional 가중, source_payload 종목별 audit, 전략 vs 패시브 노출. ✓
- **에러 처리(spec §6)** → realized_pnl durable txn / partial None / 벤치마크 best-effort NULL / additive 마이그레이션. ✓
- **위험(spec §8)** → 이중계상(close-only + 단일집계 테스트), drift(net_pnl이 exit_reference 무관 — Global Constraints에 근거), grain(rollup 키 `(date,product,scope,tag)`와 set_benchmark 키 일치). ✓
- **경계** → 라우터/서비스 market-data 무import, runner는 demo_scalping_exec(가드 밖). 라우터 가드 회귀 테스트 포함(Task 4 Step 5). ✓
- **스펙 정제** → §4.2 "서비스 draft 시점 계산" → "runner/CLI 저장"으로 경계-안전하게 변경(상단 노트). 측정 결과는 동일.
- **범위 밖 준수** → A/B session_tag·trade_retrospectives 마이그레이션·cron·LLM 미포함. ✓

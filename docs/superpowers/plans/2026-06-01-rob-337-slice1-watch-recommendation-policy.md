# ROB-337 Slice 1 — watch 추천 payload + 결정적 가격기준 policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** watch_only/limit_wait 항목에 대해 결정적 policy로 매수 검토 가격기준(entry_review_below_price / suggested_limit_price_range / max_chase_price / invalidation)을 산정하고, additive JSONB 컬럼 `watch_recommendation`에 저장하는 read-only MCP 도구를 추가한다.

**Architecture:** 순수 결정적 policy 모듈(`watch_recommendation_policy.py`)이 주입된 시장 evidence(현재가·일봉)로 가격기준을 계산한다. 신규 MCP 도구 `investment_watch_recommend`가 market_data를 읽어 policy를 호출하고, dry-run(기본)은 결과만 반환, `commit=True`는 verdict-gate 통과 시 item의 `watch_recommendation` 컬럼에 영속화한다. broker/order 호출 0, scheduler 0.

**Tech Stack:** Python 3.13, Pydantic v2, SQLAlchemy async + Alembic, FastMCP, pytest (`-n auto` shared Postgres), Decimal arithmetic.

**Spec:** `docs/superpowers/specs/2026-06-01-rob-337-watch-recommendation-policy-design.md`

---

## File Structure

- `app/schemas/investment_reports.py` — 신규 `WatchInvalidation` / `WatchPriceRange` / `WatchRecommendationEvidence` / `WatchRecommendationPayload`; `InvestmentReportItemResponse.watch_recommendation` 필드.
- `app/services/investment_reports/watch_recommendation_policy.py` (신규) — `WatchPolicyInput` + 상수 + `compute_watch_recommendation()` 순수 함수.
- `app/models/investment_reports.py` — `InvestmentReportItem.watch_recommendation` JSONB 컬럼.
- `alembic/versions/rob337_add_watch_recommendation.py` (신규) — additive nullable 컬럼.
- `app/services/investment_reports/repository.py` — `update_item_watch_recommendation` DAO.
- `app/mcp_server/tooling/investment_reports_handlers.py` — `investment_watch_recommend_impl` + 등록 + `INVESTMENT_REPORT_TOOL_NAMES`.
- `tests/test_watch_recommendation_policy.py` (신규) — policy + 스키마 단위.
- `tests/test_investment_reports_mcp.py` — MCP 도구 동작/게이트/no-mutation.

---

## Task 1: 추천 스키마 모델 (TDD)

**Files:**
- Test: `tests/test_watch_recommendation_policy.py` (신규)
- Modify: `app/schemas/investment_reports.py`

- [ ] **Step 1: 실패 테스트 작성**

새 파일 `tests/test_watch_recommendation_policy.py`:

```python
"""ROB-337 Slice 1 — watch recommendation schema + deterministic policy."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.schemas.investment_reports import (
    WatchInvalidation,
    WatchPriceRange,
    WatchRecommendationEvidence,
    WatchRecommendationPayload,
)


def _evidence() -> WatchRecommendationEvidence:
    return WatchRecommendationEvidence(lookback_days=20)


def test_price_range_rejects_low_above_high() -> None:
    with pytest.raises(ValueError):
        WatchPriceRange(low=Decimal("10"), high=Decimal("9"))


def test_invalidation_price_below_requires_price() -> None:
    with pytest.raises(ValueError):
        WatchInvalidation(kind="price_below")


def test_invalidation_condition_text_requires_text() -> None:
    with pytest.raises(ValueError):
        WatchInvalidation(kind="condition_text")


def test_payload_ok_requires_price_fields() -> None:
    with pytest.raises(ValueError):
        WatchRecommendationPayload(
            watch_reason="r",
            data_state="ok",
            source_evidence=_evidence(),
            policy_version="v1",
            computed_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            # entry_review_below_price etc. missing -> reject
        )


def test_payload_data_gap_allows_null_prices() -> None:
    payload = WatchRecommendationPayload(
        watch_reason="insufficient daily candles",
        data_state="data_gap",
        source_evidence=_evidence(),
        policy_version="v1",
        computed_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    assert payload.entry_review_below_price is None
    assert payload.data_state == "data_gap"
```

- [ ] **Step 2: 실행 → 실패 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-337 && uv run pytest tests/test_watch_recommendation_policy.py -p no:randomly -q`
Expected: FAIL — `ImportError: cannot import name 'WatchInvalidation'` (스키마 미정의).

- [ ] **Step 3: 스키마 추가**

`app/schemas/investment_reports.py`에서 `ActivateWatchRequest` 클래스 정의 바로 위(현 라인 310 부근, `class ActivateWatchRequest` 앞)에 추가:

```python
class WatchInvalidation(BaseModel):
    """ROB-337 — when a dip-buy watch thesis is invalidated."""

    kind: Literal["price_below", "condition_text"]
    price: Decimal | None = None
    text: str | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _check_kind(self) -> WatchInvalidation:
        if self.kind == "price_below" and self.price is None:
            raise ValueError("invalidation kind='price_below' requires price")
        if self.kind == "condition_text" and not self.text:
            raise ValueError("invalidation kind='condition_text' requires text")
        return self


class WatchPriceRange(BaseModel):
    """ROB-337 — suggested limit price band [low, high]."""

    low: Decimal
    high: Decimal

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _check_order(self) -> WatchPriceRange:
        if self.low > self.high:
            raise ValueError("WatchPriceRange.low must be <= high")
        return self


class WatchRecommendationEvidence(BaseModel):
    """ROB-337 — deterministic evidence behind a watch recommendation."""

    support: Decimal | None = None
    resistance: Decimal | None = None
    spread_bps: Decimal | None = None
    volatility_pct: Decimal | None = None
    lookback_days: int
    news_ref: str | None = None
    screener_reason: str | None = None

    model_config = ConfigDict(extra="forbid")


class WatchRecommendationPayload(BaseModel):
    """ROB-337 Slice 1 — advisory price-review thresholds for a watch item.

    Persisted as JSONB in ``investment_report_items.watch_recommendation``.
    Advisory only — no order is created or submitted from this payload.
    """

    watch_reason: str
    data_state: Literal["ok", "data_gap"]
    reference_price: Decimal | None = None
    entry_review_below_price: Decimal | None = None
    suggested_limit_price_range: WatchPriceRange | None = None
    max_chase_price: Decimal | None = None
    invalidation: WatchInvalidation | None = None
    expiry_at: datetime | None = None
    review_cadence: str = "daily"
    source_evidence: WatchRecommendationEvidence
    policy_version: str
    computed_at: datetime

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _check_ok_completeness(self) -> WatchRecommendationPayload:
        if self.data_state == "ok":
            missing = [
                name
                for name, val in (
                    ("reference_price", self.reference_price),
                    ("entry_review_below_price", self.entry_review_below_price),
                    ("suggested_limit_price_range", self.suggested_limit_price_range),
                    ("max_chase_price", self.max_chase_price),
                    ("invalidation", self.invalidation),
                )
                if val is None
            ]
            if missing:
                raise ValueError(f"data_state='ok' requires {missing}")
        return self
```

(`Literal`, `Decimal`, `datetime`, `BaseModel`, `ConfigDict`, `model_validator`는 파일 상단에 이미 import 되어 있음.)

- [ ] **Step 4: 실행 → 통과 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-337 && uv run pytest tests/test_watch_recommendation_policy.py -p no:randomly -q`
Expected: PASS (5 tests).

- [ ] **Step 5: 커밋**

```bash
cd /Users/mgh3326/work/auto_trader.rob-337
git add app/schemas/investment_reports.py tests/test_watch_recommendation_policy.py
git commit -m "feat(ROB-337): WatchRecommendationPayload schema + validators

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 2: 결정적 policy 모듈 (TDD)

**Files:**
- Create: `app/services/investment_reports/watch_recommendation_policy.py`
- Test: `tests/test_watch_recommendation_policy.py` (확장)

- [ ] **Step 1: 실패 테스트 추가**

`tests/test_watch_recommendation_policy.py` 하단에 추가:

```python
from app.services.investment_reports.watch_recommendation_policy import (
    LOOKBACK_DAYS,
    POLICY_VERSION,
    VOL_FLOOR,
    WatchPolicyInput,
    compute_watch_recommendation,
)

_NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _flat_input(n: int = 25, price: str = "100") -> WatchPolicyInput:
    p = Decimal(price)
    return WatchPolicyInput(
        reference_price=p,
        best_bid=None,
        best_ask=None,
        daily_highs=[p] * n,
        daily_lows=[p] * n,
        daily_closes=[p] * n,
    )


def test_policy_flat_series_exact() -> None:
    rec = compute_watch_recommendation(_flat_input(), computed_at=_NOW)
    assert rec.data_state == "ok"
    assert rec.policy_version == POLICY_VERSION
    assert rec.source_evidence.volatility_pct == VOL_FLOOR  # 0.02 floor
    assert rec.source_evidence.support == Decimal("100")
    # raw_entry=98, support_floor=100.5 -> clamp to reference 100
    assert rec.entry_review_below_price == Decimal("100")
    assert rec.max_chase_price == Decimal("100")          # min(100, 100*1.005)
    assert rec.invalidation.kind == "price_below"
    assert rec.invalidation.price == Decimal("98.000")    # 100*(1-0.02)
    # range collapses (low 100.5 > high 100 -> low=high=high)
    assert rec.suggested_limit_price_range.low == rec.suggested_limit_price_range.high


def test_policy_support_below_price_inequalities() -> None:
    # support=80, resistance=115, varied -> vol>floor
    lows = [Decimal(x) for x in [95, 92, 90, 88, 85, 83, 80, 82, 84, 86,
                                 88, 90, 91, 89, 87, 85, 83, 84, 86, 88,
                                 90, 92, 94, 95, 96]]
    highs = [low + Decimal("15") for low in lows]
    closes = [low + Decimal("5") for low in lows]
    inp = WatchPolicyInput(
        reference_price=Decimal("100"),
        best_bid=None,
        best_ask=None,
        daily_highs=highs,
        daily_lows=lows,
        daily_closes=closes,
    )
    rec = compute_watch_recommendation(inp, computed_at=_NOW)
    assert rec.data_state == "ok"
    assert rec.source_evidence.support == Decimal("80")
    assert rec.entry_review_below_price < Decimal("100")          # below current
    assert rec.suggested_limit_price_range.low <= rec.suggested_limit_price_range.high
    assert rec.max_chase_price <= Decimal("100")                  # no chase above current
    assert rec.invalidation.price < Decimal("80")                 # below support


def test_policy_data_gap_when_too_few_candles() -> None:
    inp = WatchPolicyInput(
        reference_price=Decimal("100"),
        best_bid=None,
        best_ask=None,
        daily_highs=[Decimal("100")] * 10,
        daily_lows=[Decimal("100")] * 10,
        daily_closes=[Decimal("100")] * 10,
    )
    rec = compute_watch_recommendation(inp, computed_at=_NOW)
    assert rec.data_state == "data_gap"
    assert rec.entry_review_below_price is None
    assert rec.max_chase_price is None
    assert rec.invalidation is None
    assert rec.source_evidence.lookback_days == LOOKBACK_DAYS


def test_policy_expiry_uses_valid_until_when_given() -> None:
    vu = datetime(2026, 6, 20, tzinfo=timezone.utc)
    rec = compute_watch_recommendation(_flat_input(), computed_at=_NOW, valid_until=vu)
    assert rec.expiry_at == vu
```

- [ ] **Step 2: 실행 → 실패 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-337 && uv run pytest tests/test_watch_recommendation_policy.py -p no:randomly -q`
Expected: FAIL — `ModuleNotFoundError: ...watch_recommendation_policy`.

- [ ] **Step 3: policy 모듈 구현**

새 파일 `app/services/investment_reports/watch_recommendation_policy.py`:

```python
"""ROB-337 Slice 1 — deterministic watch recommendation policy.

Pure functions: given injected market evidence (reference price + daily
OHLC arrays, ordered oldest->newest), compute advisory buy-review price
thresholds. No DB / network access; ``computed_at`` is injected so the
output is fully deterministic and unit-testable. Advisory only — never
produces an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.schemas.investment_reports import (
    WatchInvalidation,
    WatchPriceRange,
    WatchRecommendationEvidence,
    WatchRecommendationPayload,
)

POLICY_VERSION = "v1"
LOOKBACK_DAYS = 20
ATR_PERIOD = 14
VOL_FLOOR = Decimal("0.02")
K_ENTRY = Decimal("1.0")
SUPPORT_BUFFER = Decimal("0.005")
CHASE_BUFFER = Decimal("0.005")
INVAL_FLOOR = Decimal("0.02")
DEFAULT_HORIZON_DAYS = 14


@dataclass(frozen=True)
class WatchPolicyInput:
    """Evidence for the policy. OHLC lists ordered oldest->newest."""

    reference_price: Decimal | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    daily_highs: list[Decimal]
    daily_lows: list[Decimal]
    daily_closes: list[Decimal]
    news_ref: str | None = None
    screener_reason: str | None = None


def _atr_pct(inp: WatchPolicyInput) -> Decimal:
    """ATR(14)/reference_price, floored at VOL_FLOOR. Inputs are guaranteed
    long enough by the data_gap gate (LOOKBACK_DAYS > ATR_PERIOD)."""
    highs = inp.daily_highs[-(ATR_PERIOD + 1):]
    lows = inp.daily_lows[-(ATR_PERIOD + 1):]
    closes = inp.daily_closes[-(ATR_PERIOD + 1):]
    trs: list[Decimal] = []
    for i in range(1, len(highs)):
        prev_close = closes[i - 1]
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - prev_close),
            abs(lows[i] - prev_close),
        )
        trs.append(tr)
    if not trs or inp.reference_price in (None, Decimal("0")):
        return VOL_FLOOR
    atr = sum(trs, Decimal("0")) / Decimal(len(trs))
    pct = atr / inp.reference_price
    return max(pct, VOL_FLOOR)


def _is_data_gap(inp: WatchPolicyInput) -> bool:
    return (
        inp.reference_price is None
        or len(inp.daily_lows) < LOOKBACK_DAYS
        or len(inp.daily_highs) < LOOKBACK_DAYS
        or len(inp.daily_closes) < LOOKBACK_DAYS
    )


def compute_watch_recommendation(
    inp: WatchPolicyInput,
    *,
    computed_at: datetime,
    valid_until: datetime | None = None,
    watch_reason_prefix: str = "dip-buy review",
) -> WatchRecommendationPayload:
    if _is_data_gap(inp):
        return WatchRecommendationPayload(
            watch_reason=(
                f"data_gap: need >= {LOOKBACK_DAYS} daily candles and a "
                "reference price to compute thresholds"
            ),
            data_state="data_gap",
            source_evidence=WatchRecommendationEvidence(
                lookback_days=LOOKBACK_DAYS,
                news_ref=inp.news_ref,
                screener_reason=inp.screener_reason,
            ),
            policy_version=POLICY_VERSION,
            computed_at=computed_at,
            expiry_at=valid_until,
        )

    reference_price = inp.reference_price
    assert reference_price is not None  # guarded by _is_data_gap
    support = min(inp.daily_lows[-LOOKBACK_DAYS:])
    resistance = max(inp.daily_highs[-LOOKBACK_DAYS:])
    vol = _atr_pct(inp)

    support_floor = support * (Decimal("1") + SUPPORT_BUFFER)
    raw_entry = reference_price * (Decimal("1") - K_ENTRY * vol)
    entry = min(max(raw_entry, support_floor), reference_price)

    range_low = support_floor
    range_high = entry
    if range_low > range_high:
        range_low = range_high

    max_chase = min(reference_price, entry * (Decimal("1") + CHASE_BUFFER))
    inval_price = support * (Decimal("1") - max(vol, INVAL_FLOOR))

    expiry = valid_until or (computed_at + timedelta(days=DEFAULT_HORIZON_DAYS))

    return WatchRecommendationPayload(
        watch_reason=(
            f"{watch_reason_prefix}: review below {entry} toward {support_floor} "
            f"support; vol {vol} (frac); invalid below {inval_price}"
        ),
        data_state="ok",
        reference_price=reference_price,
        entry_review_below_price=entry,
        suggested_limit_price_range=WatchPriceRange(low=range_low, high=range_high),
        max_chase_price=max_chase,
        invalidation=WatchInvalidation(kind="price_below", price=inval_price),
        expiry_at=expiry,
        review_cadence="daily",
        source_evidence=WatchRecommendationEvidence(
            support=support,
            resistance=resistance,
            spread_bps=None,  # v1: Quote has no bid/ask; orderbook fetch deferred
            volatility_pct=vol,
            lookback_days=LOOKBACK_DAYS,
            news_ref=inp.news_ref,
            screener_reason=inp.screener_reason,
        ),
        policy_version=POLICY_VERSION,
        computed_at=computed_at,
    )
```

> Note: spec §4.3가 언급한 stdev fallback은 구현하지 않는다 — `LOOKBACK_DAYS(20) > ATR_PERIOD(14)+1`이라 data_gap 게이트를 통과하면 ATR 입력이 항상 충분하다(dead-code 회피). spread_bps는 `Quote`에 bid/ask가 없어 v1에서 None.

- [ ] **Step 4: 실행 → 통과 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-337 && uv run pytest tests/test_watch_recommendation_policy.py -p no:randomly -q`
Expected: PASS (9 tests).

- [ ] **Step 5: 커밋**

```bash
cd /Users/mgh3326/work/auto_trader.rob-337
git add app/services/investment_reports/watch_recommendation_policy.py tests/test_watch_recommendation_policy.py
git commit -m "feat(ROB-337): deterministic watch recommendation policy (v1)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 3: 모델 컬럼 + 마이그레이션

**Files:**
- Modify: `app/models/investment_reports.py` (`InvestmentReportItem`, 현 라인 324 부근의 `watch_condition` 컬럼 다음)
- Create: `alembic/versions/rob337_add_watch_recommendation.py`

- [ ] **Step 1: ORM 컬럼 추가**

`app/models/investment_reports.py`의 `InvestmentReportItem`에서 `watch_condition` 컬럼 정의(현 라인 324) 다음 줄에 추가:

```python
    # ROB-337 — advisory buy-review price thresholds (deterministic policy
    # output). Distinct from watch_condition (the scanner trigger contract).
    watch_recommendation: Mapped[dict | None] = mapped_column(JSONB)
```

(`JSONB`, `Mapped`, `mapped_column`은 이미 import 되어 있음.)

- [ ] **Step 2: 마이그레이션 파일 작성**

새 파일 `alembic/versions/rob337_add_watch_recommendation.py`:

```python
"""ROB-337 add investment_report_items.watch_recommendation

Revision ID: rob337_add_watch_recommendation
Revises: 14fa36b85d0a
Create Date: 2026-06-01

Additive nullable JSONB column for advisory buy-review price thresholds.
Existing rows keep NULL. No CHECK. Production apply is operator-gated.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "rob337_add_watch_recommendation"
down_revision: Union[str, Sequence[str], None] = "14fa36b85d0a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "investment_report_items",
        sa.Column("watch_recommendation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema="review",
    )


def downgrade() -> None:
    op.drop_column("investment_report_items", "watch_recommendation", schema="review")
```

- [ ] **Step 3: 마이그레이션 적용 + 단일 head 확인**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-337
uv run alembic upgrade head && uv run alembic heads
```
Expected: 에러 없이 적용, `alembic heads`가 단일 head `rob337_add_watch_recommendation` 출력.

- [ ] **Step 4: 모델↔DB 정합 확인 (autogenerate diff 없음)**

Run: `cd /Users/mgh3326/work/auto_trader.rob-337 && uv run alembic revision --autogenerate -m "probe" 2>&1 | grep -i "add_column\|drop_column\|watch_recommendation" || echo "NO DIFF"`
Expected: `NO DIFF` (정합). 생성된 probe 파일이 있으면 삭제: `rm alembic/versions/*probe*.py 2>/dev/null` (revision 파일 내 "probe" 메시지로 식별).

- [ ] **Step 5: 커밋**

```bash
cd /Users/mgh3326/work/auto_trader.rob-337
git add app/models/investment_reports.py alembic/versions/rob337_add_watch_recommendation.py
git commit -m "feat(ROB-337): add investment_report_items.watch_recommendation column (additive)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 4: 리포지토리 DAO + 응답 필드

**Files:**
- Modify: `app/services/investment_reports/repository.py` (`update_item_watch_condition` 다음)
- Modify: `app/schemas/investment_reports.py` (`InvestmentReportItemResponse`, 현 라인 385 `watch_condition` 다음)

- [ ] **Step 1: DAO 메서드 추가**

`app/services/investment_reports/repository.py`의 `update_item_watch_condition` 메서드 정의 다음에 추가:

```python
    async def update_item_watch_recommendation(
        self, item_id: int, watch_recommendation: dict
    ) -> None:
        """ROB-337 — persist the advisory watch_recommendation JSONB onto an
        item. Flushes but never commits (caller owns the transaction)."""
        await self._session.execute(
            sa.update(InvestmentReportItem)
            .where(InvestmentReportItem.id == item_id)
            .values(watch_recommendation=watch_recommendation)
        )
```

- [ ] **Step 2: 응답 스키마 필드 추가**

`app/schemas/investment_reports.py`의 `InvestmentReportItemResponse`에서 `watch_condition: dict[str, Any] | None`(현 라인 385) 다음 줄에 추가:

```python
    watch_recommendation: dict[str, Any] | None = None
```

- [ ] **Step 3: import/구문 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-337 && uv run python -c "from app.services.investment_reports.repository import InvestmentReportsRepository; from app.schemas.investment_reports import InvestmentReportItemResponse; print(hasattr(InvestmentReportsRepository,'update_item_watch_recommendation'), 'watch_recommendation' in InvestmentReportItemResponse.model_fields)"`
Expected: `True True`

- [ ] **Step 4: 커밋**

```bash
cd /Users/mgh3326/work/auto_trader.rob-337
git add app/services/investment_reports/repository.py app/schemas/investment_reports.py
git commit -m "feat(ROB-337): repo.update_item_watch_recommendation + response field

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 5: MCP 도구 `investment_watch_recommend` (TDD)

**Files:**
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py`
- Test: `tests/test_investment_reports_mcp.py`

- [ ] **Step 1: 실패 테스트 추가**

`tests/test_investment_reports_mcp.py` 상단 import 블록에 핸들러 추가 (현 라인 17-26 import 묶음에 한 줄):

```python
    investment_watch_recommend_impl,
```

그리고 파일 하단(마지막 테스트 뒤)에 추가:

```python
@pytest.fixture
def _stub_market_data(monkeypatch):
    """Stub market_data so the watch-recommend tool needs no live network and
    we can assert it touches no broker/order client."""
    from app.mcp_server.tooling import investment_reports_handlers as h
    from app.services.market_data.contracts import Candle

    async def fake_get_quote(symbol, market):
        from app.services.market_data.contracts import Quote

        return Quote(symbol=symbol, market=market, price=100.0, source="stub")

    async def fake_get_ohlcv(symbol, market, period, count, end=None):
        import datetime as _dt

        return [
            Candle(
                symbol=symbol, market=market, source="stub", period="day",
                timestamp=_dt.datetime(2026, 5, d + 1, tzinfo=_dt.timezone.utc),
                open=100.0, high=102.0, low=98.0, close=100.0, volume=1.0,
            )
            for d in range(25)
        ]

    monkeypatch.setattr(h.market_data_service, "get_quote", fake_get_quote)
    monkeypatch.setattr(h.market_data_service, "get_ohlcv", fake_get_ohlcv)


@pytest.mark.asyncio
async def test_watch_recommend_dry_run_does_not_persist(
    session: AsyncSession, _stub_market_data
) -> None:
    resp = await investment_watch_recommend_impl(symbol="005930", market="kr")
    assert resp["success"] is True
    assert resp["committed"] is False
    assert resp["recommendation"]["data_state"] == "ok"
    assert resp["recommendation"]["policy_version"] == "v1"


@pytest.mark.asyncio
async def test_watch_recommend_commit_persists_on_watch_only(
    session: AsyncSession, _stub_market_data
) -> None:
    # watch_only item via evidence_snapshot.action_verdict
    item = dict(_review_watch_item_dict())
    item["evidence_snapshot"] = {"action_verdict": "watch_only"}
    created = await investment_report_create_impl(items=[item], **_create_kwargs())
    bundle = await investment_report_get_impl(created["report"]["report_uuid"])
    item_uuid = bundle["items"][0]["item_uuid"]

    resp = await investment_watch_recommend_impl(
        symbol="005930", market="kr", item_uuid=item_uuid, commit=True, actor="op"
    )
    assert resp["committed"] is True

    bundle_post = await investment_report_get_impl(created["report"]["report_uuid"])
    rec = bundle_post["items"][0]["watch_recommendation"]
    assert rec is not None
    assert rec["data_state"] == "ok"
    assert rec["entry_review_below_price"] is not None


@pytest.mark.asyncio
async def test_watch_recommend_commit_rejected_for_non_watch_verdict(
    session: AsyncSession, _stub_market_data
) -> None:
    item = dict(_review_watch_item_dict())
    item["evidence_snapshot"] = {"action_verdict": "buy_review"}  # not watch_only/limit_wait
    created = await investment_report_create_impl(items=[item], **_create_kwargs())
    bundle = await investment_report_get_impl(created["report"]["report_uuid"])
    item_uuid = bundle["items"][0]["item_uuid"]

    with pytest.raises(ValueError) as exc:
        await investment_watch_recommend_impl(
            symbol="005930", market="kr", item_uuid=item_uuid, commit=True, actor="op"
        )
    assert "watch_only" in str(exc.value) or "limit_wait" in str(exc.value)


@pytest.mark.asyncio
async def test_watch_recommend_commit_rejected_on_data_gap(
    session: AsyncSession, monkeypatch
) -> None:
    from app.mcp_server.tooling import investment_reports_handlers as h
    from app.services.market_data.contracts import Quote

    async def fake_get_quote(symbol, market):
        return Quote(symbol=symbol, market=market, price=100.0, source="stub")

    async def few_candles(symbol, market, period, count, end=None):
        return []  # data gap

    monkeypatch.setattr(h.market_data_service, "get_quote", fake_get_quote)
    monkeypatch.setattr(h.market_data_service, "get_ohlcv", few_candles)

    item = dict(_review_watch_item_dict())
    item["evidence_snapshot"] = {"action_verdict": "watch_only"}
    created = await investment_report_create_impl(items=[item], **_create_kwargs())
    bundle = await investment_report_get_impl(created["report"]["report_uuid"])
    item_uuid = bundle["items"][0]["item_uuid"]

    with pytest.raises(ValueError) as exc:
        await investment_watch_recommend_impl(
            symbol="005930", market="kr", item_uuid=item_uuid, commit=True, actor="op"
        )
    assert "data_gap" in str(exc.value)
```

- [ ] **Step 2: tool-name 집합 테스트 갱신**

`tests/test_investment_reports_mcp.py`의 `test_tool_names_match_registered_set`(현 라인 94-104)의 set에 추가:

```python
        "investment_watch_recommend",
```

- [ ] **Step 3: 실행 → 실패 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-337 && uv run pytest tests/test_investment_reports_mcp.py -p no:randomly -q -k "watch_recommend or tool_names"`
Expected: FAIL — `ImportError: cannot import name 'investment_watch_recommend_impl'`.

- [ ] **Step 4: 핸들러 구현**

`app/mcp_server/tooling/investment_reports_handlers.py` 상단 import에 추가 (현 라인 17 `from app.core.db import AsyncSessionLocal` 다음):

```python
from datetime import datetime, timezone

from app.services import market_data as market_data_service
from app.services.investment_reports.watch_recommendation_policy import (
    ATR_PERIOD,
    LOOKBACK_DAYS,
    WatchPolicyInput,
    compute_watch_recommendation,
)
```

(핸들러는 `WatchRecommendationPayload` 타입을 직접 참조하지 않고 policy가 반환한 인스턴스만 사용하므로 import 불필요. `datetime`/`timezone`이 파일에 이미 import 되어 있으면 중복 줄은 생략.)

그리고 `investment_report_activate_watch_impl` 함수 정의 다음(현 라인 411 부근, 다음 `# ---` 블록 앞)에 추가:

```python
# ---------------------------------------------------------------------------
# investment_watch_recommend (ROB-337 Slice 1)
# ---------------------------------------------------------------------------
_RECOMMEND_VERDICTS = {"watch_only", "limit_wait"}
_MARKET_MAP = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}


def _normalize_recommend_symbol(symbol: str, market: str) -> str:
    s = str(symbol or "").strip()
    if market == "crypto":
        up = s.upper()
        return up if "-" in up else f"KRW-{up}"
    if market == "us":
        return s.upper()
    return s


async def investment_watch_recommend_impl(
    symbol: str,
    market: str,
    item_uuid: str | None = None,
    commit: bool = False,
    actor: str | None = None,
) -> dict:
    """ROB-337 — compute advisory buy-review price thresholds for a watch.

    Read-only by default (commit=False). Advisory only: NO order is created
    or submitted. commit=True persists onto the item's watch_recommendation
    column, gated on action_verdict in {watch_only, limit_wait} and a
    non-data_gap result.
    """
    if market not in _MARKET_MAP:
        return {"success": False, "error": "unsupported_market", "market": market}

    md_symbol = _normalize_recommend_symbol(symbol, market)
    md_market = _MARKET_MAP[market]

    quote = await market_data_service.get_quote(symbol=md_symbol, market=md_market)
    reference_price = (
        Decimal(str(quote.price)) if getattr(quote, "price", None) is not None else None
    )
    candles = await market_data_service.get_ohlcv(
        symbol=md_symbol,
        market=md_market,
        period="day",
        count=LOOKBACK_DAYS + ATR_PERIOD + 6,
    )
    ordered = sorted(candles, key=lambda c: c.timestamp)
    highs = [Decimal(str(c.high)) for c in ordered]
    lows = [Decimal(str(c.low)) for c in ordered]
    closes = [Decimal(str(c.close)) for c in ordered]

    valid_until = None
    async with AsyncSessionLocal() as db:
        repo = InvestmentReportsRepository(db)
        item = None
        if item_uuid is not None:
            item = await repo.get_item_by_uuid(UUID(item_uuid))
            if item is not None:
                valid_until = item.valid_until

        payload = compute_watch_recommendation(
            WatchPolicyInput(
                reference_price=reference_price,
                best_bid=None,
                best_ask=None,
                daily_highs=highs,
                daily_lows=lows,
                daily_closes=closes,
            ),
            computed_at=datetime.now(timezone.utc),
            valid_until=valid_until,
        )
        rec_json = payload.model_dump(mode="json")

        if not commit:
            return {"success": True, "committed": False, "recommendation": rec_json}

        if item_uuid is None or item is None:
            raise ValueError("commit=True requires an existing item_uuid")
        verdict = None
        if isinstance(item.evidence_snapshot, dict):
            verdict = item.evidence_snapshot.get("action_verdict")
        if verdict not in _RECOMMEND_VERDICTS:
            raise ValueError(
                "commit requires item action_verdict in {watch_only, limit_wait}; "
                f"got {verdict!r}"
            )
        if payload.data_state == "data_gap":
            raise ValueError("refusing to commit a data_gap recommendation")

        await repo.update_item_watch_recommendation(item.id, rec_json)
        await db.commit()
        return {
            "success": True,
            "committed": True,
            "item_uuid": item_uuid,
            "recommendation": rec_json,
        }
```

- [ ] **Step 5: 도구 등록**

`register_investment_report_tools`(현 라인 706~)의 마지막 `mcp.tool(...)(investment_report_generate_from_bundle_impl)` 다음에 추가:

```python
    mcp.tool(
        name="investment_watch_recommend",
        description=(
            "ROB-337 — compute advisory buy-review price thresholds "
            "(entry_review_below_price, suggested_limit_price_range, "
            "max_chase_price, invalidation) for a symbol from deterministic "
            "market evidence. Read-only by default; commit=True persists onto "
            "an item's watch_recommendation (gated on action_verdict in "
            "{watch_only, limit_wait}, refused on data_gap). Advisory only — "
            "no order is created or submitted."
        ),
    )(investment_watch_recommend_impl)
```

그리고 `INVESTMENT_REPORT_TOOL_NAMES` 집합(현 라인 50 부근 정의)에 `"investment_watch_recommend"` 추가, `__all__`에 `"investment_watch_recommend_impl"` 추가.

- [ ] **Step 6: 실행 → 통과 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-337 && uv run pytest tests/test_investment_reports_mcp.py -p no:randomly -q`
Expected: PASS (기존 + 신규 watch_recommend 4 + tool_names).

- [ ] **Step 7: 커밋**

```bash
cd /Users/mgh3326/work/auto_trader.rob-337
git add app/mcp_server/tooling/investment_reports_handlers.py tests/test_investment_reports_mcp.py
git commit -m "feat(ROB-337): investment_watch_recommend MCP tool (dry-run default, gated commit)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 6: 전체 검증 + lint

**Files:** 없음 (품질 게이트만)

- [ ] **Step 1: 관련 테스트 전체 실행**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-337
uv run pytest tests/test_watch_recommendation_policy.py tests/test_investment_reports_mcp.py tests/test_investment_reports_model.py -p no:randomly -q
```
Expected: 전부 PASS.

- [ ] **Step 2: lint (CI 게이트와 동일)**

Run: `cd /Users/mgh3326/work/auto_trader.rob-337 && uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/`
Expected: 둘 다 통과. (format 실패 시 `uv run ruff format app/ tests/` 후 변경분 amend.)

- [ ] **Step 3: 타입 체크**

Run: `cd /Users/mgh3326/work/auto_trader.rob-337 && uv run ty check app/services/investment_reports/watch_recommendation_policy.py app/schemas/investment_reports.py app/mcp_server/tooling/investment_reports_handlers.py`
Expected: 본 변경이 새로 만든 에러 없음.

- [ ] **Step 4: 정리 커밋 (필요 시)**

```bash
cd /Users/mgh3326/work/auto_trader.rob-337
git add -A && git commit -m "chore(ROB-337): lint/format

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Post-implementation (PR 외 수동 단계)

- PR 전 사전-머지 full-CI 게이트: `ruff check app/ tests/` + import guards + GitHub Test 워크플로우 green 확인 후 머지.
- 마이그레이션은 PR에 포함하되 **production 적용(`alembic upgrade head`)은 operator-gated** — PR 설명에 명시.
- Linear ROB-337 댓글: Slice 1 완료(payload+policy), no broker/order/order-intent mutation + no scheduler 경계, 테스트/CI evidence, **Slice 2(review job) seam은 watch_recommendation 컬럼**임을 기록. ROB-337은 Slice 2까지 끝나야 Done.

## 비범위 (재확인)

- review job / keep·reprice·expire·review_now·data_gap 분류 / 알림 throttling = **Slice 2 (별도 스펙·플랜)**.
- sell-side / index / fx 추천, orderbook spread 산출(Quote에 bid/ask 없음) = 비범위.
- ROB-403(zone/구조화 조건)와 독립.

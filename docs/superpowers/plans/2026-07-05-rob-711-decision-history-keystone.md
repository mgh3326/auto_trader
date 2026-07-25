# ROB-711 decision_history 주입 (키스톤) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 심볼 분석(`analyze_stock_batch`) 응답에 그 심볼의 과거 판단→결과(`decision_history`)를 결정적으로 자동 주입해, 매 세션 새로 태어나는 분석 LLM이 과거 판단·체결·교훈·미해소 주장·캘리브레이션을 보고 판단하도록 한다.

**Architecture:** 신규 read-only 서비스 `app/services/decision_history.py`의 `build_decision_context(db, symbol, market, setup_tag=None) -> dict | None`. 기존 스파인(investment_report_items + live 레저 3종 + trade_forecasts + trade_retrospectives)을 **symbol + 최근성(recency) 창**으로 조인한다(오늘 `report_item_uuid`는 사실상 0% 적재라 "exact" 조인은 무효 — `link_quality:"symbol_window"` 고정, exact 승격은 ROB-714). 스모크/테스트 행을 필터하고, Brier는 기존 `build_forecast_calibration_aggregate`를 재사용하며, 표본 n<10이면 `insufficient_sample`로 정직하게 방출한다. 주입은 기존 `_attach_fresh_artifact_hints`와 동일한 **배치·fail-open 포스트패스** `_attach_decision_history`로 `analyze_stock_batch_impl`에 배선한다.

**Tech Stack:** Python 3.13, SQLAlchemy async, pytest-asyncio (`db_session` fixture at `tests/conftest.py:608`), 기존 `AsyncSessionLocal`.

## Global Constraints

- **ROB-501**: `app/**`에서 in-process LLM provider 임포트/인스턴스화 금지 — 전부 결정적 집계/read.
- **Read-path 전용**: 주문 hot path 무접촉. **스키마 변경 0**(migration 없음).
- **Fail-open**: 어떤 DB/조회 예외도 결과를 건드리지 않는다(`decision_history`는 순수 advisory 필드). 예외는 `logger.debug`.
- **스모크 필터**: `created_by_profile` / `strategy_key` / `correlation_id` / (report item의) `rationale` 중 하나라도 `"smoke"`(대소문자 무시) 포함이면 제외. **mock/paper(`account_mode` = `kis_mock`/`upbit_live`/`paper` 등)는 제외하지 않는다** — ROB-705가 확립한 손절 학습 원천이므로 유효 데이터.
- **심볼당 상한**: `prior_decisions ≤ 6`, `prior_lessons ≤ 3`, `realized_outcomes ≤ 5`, `recent_fills ≤ 6`, `open_claims ≤ 5`. `rationale`/`lesson`은 220자 트렁케이트.
- **`running_brier_*`**: 채점된(closed+brier) 표본 n<10 → `flag:"insufficient_sample"`, `mean_brier:null`. (현 DB: scored=0이므로 항상 insufficient.)
- **`link_quality`**: 현재 `"symbol_window"` 상수. ROB-714가 place-time에 provenance 키를 심으면 exact 승격(이 플랜에서는 구현 안 함 — YAGNI, 데이터 없음).
- **심볼 정규화**: `app.services.trade_journal.forecast_service._normalize_symbol_for_filter(symbol, instrument_type)` 재사용(US dot/slash, crypto dash 인지).

## File Structure

- **Create** `app/services/decision_history.py` — 서비스 전체(모듈 함수 + 내부 헬퍼). 단일 책임: 심볼별 결정 이력 집계.
- **Create** `tests/services/test_decision_history.py` — 서비스 단위 테스트.
- **Modify** `app/mcp_server/tooling/analysis_tool_handlers.py` — `_attach_decision_history` 포스트패스 추가(`_attach_fresh_artifact_hints` 바로 뒤, `:908` 부근) + `analyze_stock_batch_impl` 배선.
- **Create** `tests/mcp_server/test_analyze_stock_batch_decision_history.py` — 주입 배선 + fail-open 테스트.

---

### Task 1: 서비스 스켈레톤 + prior_decisions (report items) + 스모크 필터

**Files:**
- Create: `app/services/decision_history.py`
- Test: `tests/services/test_decision_history.py`

**Interfaces:**
- Produces:
  - `async def build_decision_context(db: AsyncSession, symbol: str, market: str, setup_tag: str | None = None) -> dict | None`
  - `_is_smoke(*values: str | None) -> bool`
  - `_truncate(text: str | None, limit: int = 220) -> str | None`
  - 모듈 상수 `MARKET_TO_INSTRUMENT = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}`, `MAX_DECISIONS = 6`

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_decision_history.py
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReport, InvestmentReportItem
from app.services.decision_history import build_decision_context


async def _make_report(db: AsyncSession, **overrides) -> InvestmentReport:
    payload = {
        "report_uuid": uuid.uuid4(),
        "idempotency_key": f"key-{uuid.uuid4()}",
        "report_type": "kr_morning",
        "market": "kr",
        "market_session": "regular",
        "account_scope": "kis_mock",
        "execution_mode": "mock_preview",
        "created_by_profile": "test",
        "title": "t",
        "summary": "s",
        "status": "draft",
    }
    payload.update(overrides)
    row = InvestmentReport(**payload)
    db.add(row)
    await db.flush()
    return row


async def _add_item(db: AsyncSession, report_id: int, **overrides) -> None:
    payload = {
        "report_id": report_id,
        "item_uuid": uuid.uuid4(),
        "idempotency_key": f"item-{uuid.uuid4()}",
        "item_kind": "action",
        "symbol": "005930",
        "intent": "buy_review",
        "rationale": "지지선 눌림 재진입",
        "evidence_snapshot": {},
        "created_at": datetime(2026, 6, 1, tzinfo=UTC),
    }
    payload.update(overrides)
    db.add(InvestmentReportItem(**payload))
    await db.flush()


@pytest.mark.asyncio
async def test_prior_decisions_newest_first_capped_and_smoke_filtered(
    db_session: AsyncSession,
) -> None:
    report = await _make_report(db_session)
    # 8 real + 1 smoke; expect newest-6 real, smoke excluded
    for i in range(8):
        await _add_item(
            db_session,
            report.id,
            symbol="005930",
            confidence=60 + i,
            rationale=f"real decision {i}",
            created_at=datetime(2026, 6, 1 + i, tzinfo=UTC),
        )
    await _add_item(
        db_session,
        report.id,
        symbol="005930",
        rationale="Smoke-only action review item",
        created_at=datetime(2026, 6, 20, tzinfo=UTC),
    )
    await db_session.commit()

    ctx = await build_decision_context(db_session, symbol="005930", market="kr")

    assert ctx is not None
    assert ctx["symbol"] == "005930"
    assert ctx["market"] == "kr"
    assert ctx["link_quality"] == "symbol_window"
    decisions = ctx["prior_decisions"]
    assert len(decisions) == 6  # capped
    assert decisions[0]["rationale"] == "real decision 7"  # newest first
    assert all("Smoke" not in d["rationale"] for d in decisions)  # smoke excluded
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_decision_history.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.decision_history`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/decision_history.py
"""ROB-711 — deterministic past-judgment→outcome context for a symbol.

Read-only aggregation over the existing spine (investment_report_items,
live order ledgers, trade_forecasts, trade_retrospectives). No LLM (ROB-501),
no schema change, no order hot-path touch. Injected into analyze_stock_batch
responses so each fresh analysis session sees the symbol's own history.

Join reality (2026-07-05): report_item_uuid is ~0% populated on live ledgers /
forecasts / retros, so the "exact" provenance join yields nothing today — every
link is symbol + recency. link_quality is therefore "symbol_window" until
ROB-714 mints provenance keys at place-time.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReportItem
from app.services.trade_journal.forecast_service import _normalize_symbol_for_filter

MARKET_TO_INSTRUMENT = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}

MAX_DECISIONS = 6
MAX_LESSONS = 3
MAX_OUTCOMES = 5
MAX_FILLS = 6
MAX_CLAIMS = 5
_TRUNC = 220
_SMOKE_TOKENS = ("smoke",)


def _is_smoke(*values: str | None) -> bool:
    """True if any provenance/text field marks this as a test/smoke row.

    Filters our OWN test artifacts (e.g. created_by_profile HERMES_OPERATOR_SMOKE,
    strategy_key rob474_smoke_..., rationale "Smoke-only ..."). Deliberately does
    NOT key on account_mode — mock/paper rows are real practice data (ROB-705).
    """
    for v in values:
        if v and any(tok in v.lower() for tok in _SMOKE_TOKENS):
            return True
    return False


def _truncate(text: str | None, limit: int = _TRUNC) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def build_decision_context(
    db: AsyncSession,
    symbol: str,
    market: str,
    setup_tag: str | None = None,
) -> dict | None:
    """Build the decision_history payload for one symbol, or None if no signal.

    setup_tag is reserved for realized_r_by_tag (ROB-713 stage 3); unused here.
    """
    instrument_type = MARKET_TO_INSTRUMENT.get(market)
    norm = _normalize_symbol_for_filter(symbol, instrument_type)

    prior_decisions = await _prior_decisions(db, norm)

    ctx: dict[str, Any] = {
        "symbol": norm,
        "market": market,
        "link_quality": "symbol_window",
        "prior_decisions": prior_decisions,
    }
    return ctx


async def _prior_decisions(db: AsyncSession, symbol: str) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(InvestmentReportItem)
            .where(InvestmentReportItem.symbol == symbol)
            .order_by(InvestmentReportItem.created_at.desc())
        )
    ).scalars().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        if _is_smoke(r.rationale, r.status):
            continue
        out.append(
            {
                "date": r.created_at.date().isoformat() if r.created_at else None,
                "intent": r.intent,
                "side": r.side,
                "decision_bucket": r.decision_bucket,
                "confidence": float(r.confidence) if r.confidence is not None else None,
                "rationale": _truncate(r.rationale),
            }
        )
        if len(out) >= MAX_DECISIONS:
            break
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/test_decision_history.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/decision_history.py tests/services/test_decision_history.py
git commit -m "feat(ROB-711): decision_history service — prior_decisions + smoke filter"
```

---

### Task 2: prior_lessons + realized_outcomes (retrospectives, 스모크 필터)

**Files:**
- Modify: `app/services/decision_history.py`
- Test: `tests/services/test_decision_history.py`

**Interfaces:**
- Consumes: `build_decision_context`, `_is_smoke`, `_truncate` from Task 1.
- Produces: `ctx["prior_lessons"]: list[str]`, `ctx["realized_outcomes"]: list[dict]`; helper `_retrospectives(db, symbol) -> tuple[list[str], list[dict]]`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/services/test_decision_history.py
from decimal import Decimal

from app.models.review import TradeRetrospective


async def _add_retro(db: AsyncSession, **overrides) -> None:
    payload = {
        "symbol": "005930",
        "instrument_type": "equity_kr",
        "account_mode": "upbit_live",
        "outcome": "filled",
        "side": "sell",
        "strategy_key": "resistance_ladder",
        "correlation_id": f"live:{uuid.uuid4()}",
        "realized_pnl": Decimal("33914.0000"),
        "realized_pnl_currency": "KRW",
        "realized_pnl_source": "caller_supplied",
        "pnl_pct": Decimal("11.9000"),
        "trigger_type": "fill",
        "lesson": "앵커+러너 분할이 작동",
        "next_strategy": None,
    }
    payload.update(overrides)
    db.add(TradeRetrospective(**payload))
    await db.flush()


@pytest.mark.asyncio
async def test_lessons_and_outcomes_smoke_filtered_and_capped(
    db_session: AsyncSession,
) -> None:
    await _add_retro(db_session, symbol="KRW-JUP", lesson="real lesson A")
    await _add_retro(db_session, symbol="KRW-JUP", lesson="real lesson B")
    # smoke row: created_by_profile carries SMOKE marker
    await _add_retro(
        db_session,
        symbol="KRW-JUP",
        created_by_profile="HERMES_OPERATOR_SMOKE",
        strategy_key="rob474_smoke_x",
        correlation_id="rob474-smoke-x",
        lesson="correlation_id upsert is idempotent",
    )
    await db_session.commit()

    ctx = await build_decision_context(db_session, symbol="KRW-JUP", market="crypto")

    assert ctx is not None
    assert "real lesson A" in ctx["prior_lessons"]
    assert "real lesson B" in ctx["prior_lessons"]
    assert all("idempotent" not in les for les in ctx["prior_lessons"])
    assert len(ctx["realized_outcomes"]) == 2  # smoke excluded
    first = ctx["realized_outcomes"][0]
    assert first["pnl_pct"] == 11.9
    assert first["realized_pnl"] == 33914.0
    assert first["outcome"] == "filled"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_decision_history.py::test_lessons_and_outcomes_smoke_filtered_and_capped -v`
Expected: FAIL — `KeyError: 'prior_lessons'`.

- [ ] **Step 3: Write minimal implementation**

Add import and helper, and extend `build_decision_context`:

```python
# add to imports
from app.models.review import TradeRetrospective

# add helper
async def _retrospectives(
    db: AsyncSession, symbol: str
) -> tuple[list[str], list[dict[str, Any]]]:
    rows = (
        await db.execute(
            select(TradeRetrospective)
            .where(TradeRetrospective.symbol == symbol)
            .order_by(TradeRetrospective.created_at.desc())
        )
    ).scalars().all()
    lessons: list[str] = []
    outcomes: list[dict[str, Any]] = []
    for r in rows:
        if _is_smoke(r.created_by_profile, r.strategy_key, r.correlation_id, r.lesson):
            continue
        if r.lesson and r.lesson.strip() and len(lessons) < MAX_LESSONS:
            lessons.append(_truncate(r.lesson))
        if len(outcomes) < MAX_OUTCOMES:
            outcomes.append(
                {
                    "date": r.created_at.date().isoformat() if r.created_at else None,
                    "side": r.side,
                    "outcome": r.outcome,
                    "trigger_type": r.trigger_type,
                    "pnl_pct": float(r.pnl_pct) if r.pnl_pct is not None else None,
                    "realized_pnl": (
                        float(r.realized_pnl) if r.realized_pnl is not None else None
                    ),
                }
            )
    return lessons, outcomes
```

Extend `build_decision_context` (after `prior_decisions = ...`):

```python
    lessons, outcomes = await _retrospectives(db, norm)
    ctx: dict[str, Any] = {
        "symbol": norm,
        "market": market,
        "link_quality": "symbol_window",
        "prior_decisions": prior_decisions,
        "prior_lessons": lessons,
        "realized_outcomes": outcomes,
    }
    return ctx
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/test_decision_history.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/decision_history.py tests/services/test_decision_history.py
git commit -m "feat(ROB-711): decision_history — prior_lessons + realized_outcomes (smoke-filtered)"
```

---

### Task 3: recent_fills (레저 3종 UNION) + open_claims (open forecasts)

**Files:**
- Modify: `app/services/decision_history.py`
- Test: `tests/services/test_decision_history.py`

**Interfaces:**
- Consumes: Task 2 state.
- Produces: `ctx["recent_fills"]: list[dict]`, `ctx["open_claims"]: list[dict]`; helpers `_recent_fills(db, symbol) -> list[dict]`, `_open_claims(db, symbol, instrument_type) -> list[dict]`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/services/test_decision_history.py
from datetime import date

from app.models.review import KISLiveOrderLedger, TradeForecast


@pytest.mark.asyncio
async def test_recent_fills_and_open_claims(db_session: AsyncSession) -> None:
    db_session.add(
        KISLiveOrderLedger(
            trade_date=datetime(2026, 6, 10, tzinfo=UTC),
            symbol="000660",
            instrument_type="equity_kr",
            side="buy",
            order_type="limit",
            account_mode="kis_live",
            broker="kis",
            status="filled",
            lifecycle_state="filled",
            order_no="A1",
            quantity=Decimal("1"),
            filled_qty=Decimal("1"),
            avg_fill_price=Decimal("2000000"),
            target_price=Decimal("3035000"),
            stop_loss=Decimal("1888000"),
        )
    )
    db_session.add(
        TradeForecast(
            created_by="claude",
            symbol="000660",
            instrument_type="equity_kr",
            forecast_target={
                "kind": "price_target",
                "direction": "at_or_above",
                "target_price": "2463000",
            },
            probability=Decimal("0.55"),
            horizon="10 trading days",
            review_date=date(2026, 7, 17),
            status="open",
        )
    )
    await db_session.commit()

    ctx = await build_decision_context(db_session, symbol="000660", market="kr")

    assert ctx is not None
    assert len(ctx["recent_fills"]) == 1
    fill = ctx["recent_fills"][0]
    assert fill["side"] == "buy"
    assert fill["avg_fill_price"] == 2000000.0
    assert fill["stop_loss"] == 1888000.0
    assert fill["source"] == "kis"
    assert len(ctx["open_claims"]) == 1
    claim = ctx["open_claims"][0]
    assert claim["direction"] == "at_or_above"
    assert claim["target_price"] == "2463000"
    assert claim["review_date"] == "2026-07-17"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_decision_history.py::test_recent_fills_and_open_claims -v`
Expected: FAIL — `KeyError: 'recent_fills'`.

- [ ] **Step 3: Write minimal implementation**

Add imports and helpers:

```python
# add to imports
from app.models.review import (
    KISLiveOrderLedger,
    LiveOrderLedger,
    TossLiveOrderLedger,
    TradeForecast,
)


def _fill_row(source: str, r: Any) -> dict[str, Any]:
    return {
        "date": r.trade_date.date().isoformat() if r.trade_date else None,
        "side": r.side,
        "status": r.status,
        "qty": float(r.quantity) if r.quantity is not None else None,
        "filled_qty": float(r.filled_qty) if r.filled_qty is not None else None,
        "avg_fill_price": (
            float(r.avg_fill_price) if r.avg_fill_price is not None else None
        ),
        "target_price": (
            float(r.target_price) if getattr(r, "target_price", None) is not None else None
        ),
        "stop_loss": (
            float(r.stop_loss) if getattr(r, "stop_loss", None) is not None else None
        ),
        "source": source,
    }


async def _recent_fills(db: AsyncSession, symbol: str) -> list[dict[str, Any]]:
    collected: list[tuple[Any, str, Any]] = []
    for source, model in (
        ("kis", KISLiveOrderLedger),
        ("live", LiveOrderLedger),
        ("toss", TossLiveOrderLedger),
    ):
        rows = (
            await db.execute(select(model).where(model.symbol == symbol))
        ).scalars().all()
        for r in rows:
            collected.append((r.trade_date, source, r))
    # newest first across all three ledgers; None trade_date sorts last
    collected.sort(key=lambda t: (t[0] is not None, t[0]), reverse=True)
    return [_fill_row(source, r) for (_dt, source, r) in collected[:MAX_FILLS]]


async def _open_claims(db: AsyncSession, symbol: str) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(TradeForecast)
            .where(TradeForecast.symbol == symbol, TradeForecast.status == "open")
            .order_by(TradeForecast.created_at.desc())
        )
    ).scalars().all()
    out: list[dict[str, Any]] = []
    for r in rows[:MAX_CLAIMS]:
        target = r.forecast_target if isinstance(r.forecast_target, dict) else {}
        out.append(
            {
                "probability": float(r.probability) if r.probability is not None else None,
                "horizon": r.horizon,
                "review_date": r.review_date.isoformat() if r.review_date else None,
                "direction": target.get("direction"),
                "target_price": target.get("target_price"),
            }
        )
    return out
```

Extend `build_decision_context` body:

```python
    fills = await _recent_fills(db, norm)
    open_claims = await _open_claims(db, norm)
    ctx: dict[str, Any] = {
        "symbol": norm,
        "market": market,
        "link_quality": "symbol_window",
        "prior_decisions": prior_decisions,
        "prior_lessons": lessons,
        "realized_outcomes": outcomes,
        "recent_fills": fills,
        "open_claims": open_claims,
    }
    return ctx
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/test_decision_history.py -v`
Expected: PASS (all three tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/decision_history.py tests/services/test_decision_history.py
git commit -m "feat(ROB-711): decision_history — recent_fills (3 ledgers) + open_claims"
```

---

### Task 4: running_brier (심볼/전역 재사용) + insufficient_sample + empty→None

**Files:**
- Modify: `app/services/decision_history.py`
- Test: `tests/services/test_decision_history.py`

**Interfaces:**
- Consumes: Task 3 state; reuses `build_forecast_calibration_aggregate` from `app/services/trade_journal/forecast_service.py:736`.
- Produces: `ctx["running_brier_symbol"]`, `ctx["running_brier_global"]` (`{"n": int, "mean_brier": float|None, "flag": "insufficient_sample"|"ok"}`); `build_decision_context` returns **None** when no signal at all; helper `_fold_brier(agg) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/services/test_decision_history.py
@pytest.mark.asyncio
async def test_brier_insufficient_sample_and_empty_returns_none(
    db_session: AsyncSession,
) -> None:
    # symbol with zero scored forecasts → insufficient_sample
    report = await _make_report(db_session)
    await _add_item(db_session, report.id, symbol="000660", rationale="real")
    await db_session.commit()

    ctx = await build_decision_context(db_session, symbol="000660", market="kr")
    assert ctx is not None
    assert ctx["running_brier_symbol"] == {
        "n": 0,
        "mean_brier": None,
        "flag": "insufficient_sample",
    }
    assert ctx["running_brier_global"]["flag"] == "insufficient_sample"

    # a symbol with no history anywhere → None (nothing to inject)
    empty = await build_decision_context(db_session, symbol="ZZZZZ", market="kr")
    assert empty is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_decision_history.py::test_brier_insufficient_sample_and_empty_returns_none -v`
Expected: FAIL — `KeyError: 'running_brier_symbol'`.

- [ ] **Step 3: Write minimal implementation**

Add import, helper, and finalize `build_decision_context`:

```python
# add to imports
from app.services.trade_journal.forecast_service import (
    _normalize_symbol_for_filter,
    build_forecast_calibration_aggregate,
)

def _fold_brier(agg: dict[str, Any]) -> dict[str, Any]:
    groups = agg.get("groups", [])
    n = sum(int(g["sample_size"]) for g in groups)
    scored = [g for g in groups if g.get("avg_brier_score") is not None]
    denom = sum(int(g["sample_size"]) for g in scored)
    mean = (
        sum(g["avg_brier_score"] * g["sample_size"] for g in scored) / denom
        if denom
        else None
    )
    return {
        "n": n,
        "mean_brier": round(mean, 4) if mean is not None else None,
        "flag": "insufficient_sample" if n < 10 else "ok",
    }
```

Finalize `build_decision_context` (replace the assembly block):

```python
    instrument_type = MARKET_TO_INSTRUMENT.get(market)
    norm = _normalize_symbol_for_filter(symbol, instrument_type)

    prior_decisions = await _prior_decisions(db, norm)
    lessons, outcomes = await _retrospectives(db, norm)
    fills = await _recent_fills(db, norm)
    open_claims = await _open_claims(db, norm)

    if not (prior_decisions or lessons or outcomes or fills or open_claims):
        return None  # no signal — omit the field entirely

    brier_symbol = _fold_brier(
        await build_forecast_calibration_aggregate(
            db, symbol=norm, instrument_type=instrument_type
        )
    )
    brier_global = _fold_brier(await build_forecast_calibration_aggregate(db))

    return {
        "symbol": norm,
        "market": market,
        "link_quality": "symbol_window",
        "prior_decisions": prior_decisions,
        "prior_lessons": lessons,
        "realized_outcomes": outcomes,
        "recent_fills": fills,
        "open_claims": open_claims,
        "running_brier_symbol": brier_symbol,
        "running_brier_global": brier_global,
    }
```

(Remove the earlier intermediate `ctx = {...}; return ctx` blocks — this is the single final assembly.)

- [ ] **Step 4: Run tests + lint**

Run: `uv run pytest tests/services/test_decision_history.py -v`
Expected: PASS (all four tests).
Run: `uv run ruff check app/services/decision_history.py`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add app/services/decision_history.py tests/services/test_decision_history.py
git commit -m "feat(ROB-711): decision_history — running_brier (reuse aggregate) + empty→None"
```

---

### Task 5: analyze_stock_batch 주입 배선 (배치·fail-open 포스트패스)

**Files:**
- Modify: `app/mcp_server/tooling/analysis_tool_handlers.py` (add `_attach_decision_history` near `:797`/`:908`)
- Create: `tests/mcp_server/test_analyze_stock_batch_decision_history.py`

**Interfaces:**
- Consumes: `build_decision_context` (Task 4).
- Produces: `async def _attach_decision_history(results: dict[str, Any], *, market: str | None) -> None`; wired into `analyze_stock_batch_impl` right after `_attach_fresh_artifact_hints`.

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp_server/test_analyze_stock_batch_decision_history.py
from __future__ import annotations

import pytest

from app.mcp_server.tooling import analysis_tool_handlers as h


@pytest.mark.asyncio
async def test_attach_decision_history_injects_when_context_exists(monkeypatch):
    async def _fake_build(db, symbol, market, setup_tag=None):
        return {"symbol": symbol, "market": market, "prior_decisions": [{"x": 1}]}

    monkeypatch.setattr(h, "build_decision_context", _fake_build, raising=False)

    results = {"005930": {"symbol": "005930", "market_type": "kr"}}
    await h._attach_decision_history(results, market="kr")

    assert results["005930"]["decision_history"]["prior_decisions"] == [{"x": 1}]


@pytest.mark.asyncio
async def test_attach_decision_history_fail_open_on_error(monkeypatch):
    async def _boom(db, symbol, market, setup_tag=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(h, "build_decision_context", _boom, raising=False)

    results = {"005930": {"symbol": "005930", "market_type": "kr"}}
    await h._attach_decision_history(results, market="kr")  # must not raise

    assert "decision_history" not in results["005930"]  # fail-open: untouched


@pytest.mark.asyncio
async def test_attach_decision_history_skips_error_rows(monkeypatch):
    async def _fake_build(db, symbol, market, setup_tag=None):
        return {"symbol": symbol}

    monkeypatch.setattr(h, "build_decision_context", _fake_build, raising=False)

    results = {"BADSYM": {"error": "not found"}}
    await h._attach_decision_history(results, market="kr")

    assert "decision_history" not in results["BADSYM"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp_server/test_analyze_stock_batch_decision_history.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_attach_decision_history'`.

- [ ] **Step 3: Write minimal implementation**

Add the post-pass function after `_attach_fresh_artifact_hints` (near `analysis_tool_handlers.py:847`). Import `build_decision_context` at module top or lazily inside (lazy mirrors `_attach_fresh_artifact_hints`, which imports inside the try):

```python
# add near the top-level imports of analysis_tool_handlers.py
from app.services.decision_history import build_decision_context


async def _attach_decision_history(
    results: dict[str, Any],
    *,
    market: str | None,
) -> None:
    """ROB-711: inject per-symbol decision_history (past judgment→outcome).

    Batched (one session for all symbols), fail-open — any DB/lookup error
    leaves results untouched. Only the compact contract calls this. Error rows
    (dicts carrying "error") are skipped.
    """
    symbols = [
        sym
        for sym, row in results.items()
        if isinstance(row, dict) and "error" not in row
    ]
    if not symbols:
        return
    try:
        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            for sym, result in results.items():
                if not isinstance(result, dict) or "error" in result:
                    continue
                mkt = result.get("market_type") or market
                ctx = await build_decision_context(
                    db, symbol=str(sym), market=str(mkt or "")
                )
                if ctx is not None:
                    result["decision_history"] = ctx
    except Exception as exc:  # fail-open: advisory-only
        logger.debug("decision_history injection skipped: %s", exc)
```

Wire into `analyze_stock_batch_impl` (after the fresh-artifact hint call at `:908`):

```python
    if quick:
        await _attach_fresh_artifact_hints(response.get("results", {}), market=market)
        await _attach_decision_history(response.get("results", {}), market=market)
    return response
```

- [ ] **Step 4: Run tests + lint + import guard**

Run: `uv run pytest tests/mcp_server/test_analyze_stock_batch_decision_history.py tests/services/test_decision_history.py -v`
Expected: PASS (all).
Run: `uv run ruff check app/services/decision_history.py app/mcp_server/tooling/analysis_tool_handlers.py`
Expected: no errors.
Run: `uv run pytest tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py -v`
Expected: PASS (ROB-501 guard — no LLM import introduced).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/analysis_tool_handlers.py tests/mcp_server/test_analyze_stock_batch_decision_history.py
git commit -m "feat(ROB-711): inject decision_history into analyze_stock_batch (batched, fail-open)"
```

---

## Deferred (NOT in this keystone PR — 후속 이슈)

- `realized_r_by_tag`(setup별 R-multiple) → **ROB-713**(저널 집계: expectancy/R/MAE). `setup_tag` 파라미터는 지금 받아두되 미사용(forward-compat).
- `route_request` 주입 → **후속**. 현 `route_request(intent, market)`는 **symbol 파라미터가 없고** 단일 (intent,market) 라우트 플랜만 반환하므로 symbol-keyed 주입 불가 — 보유/pending 심볼에서 심볼셋을 유도하는 별도 설계 필요.
- `get_operating_briefing` 주입 → **후속**. 응답이 `OperatingBriefingResponse` Pydantic 모델로 검증되므로 새 필드는 스키마에 추가해야 함(그냥 dict에 넣으면 stripped/reject).
- `link_quality:"exact"` 승격 → **ROB-714**가 live 레저 3종에 correlation_id/report_item_uuid를 place-time에 심은 뒤. 그전까지 exact-join은 데이터가 0이라 구현 안 함(YAGNI).
- 캘리브레이션 신뢰도 게이트(Approach C) → ROB-712 negative-class 표본 ≥30건 이후 재평가.

## Success Criteria (issue와 일치)

- 출하 후 ~30 오퍼레이터 세션에서 (a) 재분석되는 **실보유·이력보유** 심볼의 80%+에서 `decision_history`가 비어있지 않음, (b) pre/post 대조(직전 30세션 vs 후 30세션: 재분석 심볼 confidence 분포·반복 실수 재발률). 안 움직이면 "주입은 지렛대 아님"을 싸게 학습 — 그것도 성공.
- **선행 게이트(코드 전)**: `scratchpad/rob-711-decision-history-pilot.md`의 3심볼(000660/005930/KRW-JUP) 수동 파일럿에서 판단 변화 관찰. 하나라도 변하면 착수, 전무하면 페이로드 설계 재고.

## Self-Review 체크

- **Spec 커버리지**: prior_decisions/prior_lessons/realized_outcomes/recent_fills/open_claims/running_brier_symbol/global/link_quality = 이슈 페이로드 필드 전부 구현(realized_r_by_tag만 ROB-713로 명시 이연). 주입 지점 = 이슈의 keystone(analyze_stock_batch) 1곳, 나머지 2곳은 결정에 따라 후속.
- **Placeholder 스캔**: 모든 스텝에 실제 코드/커맨드/기대출력 포함. TBD 없음.
- **타입 일관성**: `build_decision_context(db, symbol, market, setup_tag=None)` 시그니처가 서비스·주입 포스트패스·테스트에서 동일. `_fold_brier`는 `build_forecast_calibration_aggregate` 반환(`{"groups":[{"sample_size","avg_brier_score",...}]}`)을 정확히 소비.

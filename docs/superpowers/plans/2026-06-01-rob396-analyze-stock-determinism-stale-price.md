# ROB-396 — analyze_stock_batch 결정성 + stale price Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `analyze_stock_impl`의 source·판정 flip(증상1)과 KR stale current_price(증상2)를 ROB-397 계약 원칙으로 결정적·정직하게 고친다.

**Architecture:** (1) `analyze_stock_impl`의 research_pipeline↔legacy 비결정 분기를 제거해 항상 legacy KIS-rich 경로로 고정한다(전 시장). (2) `_apply_recommendation`에 ROB-397 fail-closed floor(`app/services/symbol_analysis/floor.py`)를 적용해 core 입력 부족 시 확신적 buy/sell을 금지한다. (3) KR current_price를 analyze 전용 라이브 오버레이(`inquire_price`)로 전환하고 `price_as_of`/`is_stale_price`를 정직하게 태그한다. 공유 `_fetch_quote_equity_kr`(orders/portfolio/screening 사용)는 건드리지 않는다.

**Tech Stack:** Python 3.13, pytest, `unittest.mock` patch, KIS `inquire_price`, ROB-397 `app/services/symbol_analysis/`(`compute_is_stale`). 새 의존성/마이그레이션 없음. `mcp_server → services` import는 허용 방향.

**참조 스펙:** `docs/superpowers/specs/2026-06-01-rob396-analyze-stock-determinism-stale-price-design.md`

---

## File Structure

- Create `app/services/symbol_analysis/floor.py` — 순수 fail-closed floor 헬퍼 (`insufficient_inputs`, `floored_action`)
- Create `tests/test_symbol_analysis_floor.py`
- Modify `app/mcp_server/tooling/analysis_analyze.py` — pipeline 분기 + dead code 제거(Task 2), `_apply_recommendation` floor 적용(Task 3), KR 라이브 quote 오버레이 배선(Task 4)
- Modify `app/mcp_server/tooling/market_data_quotes.py` — analyze 전용 `_fetch_kr_live_quote` 추가(Task 4)
- Rewrite `tests/mcp_server/test_analyze_stock_pipeline_compat.py` — 결정성 회귀로 재작성(Task 2)
- Create `tests/test_analyze_stock_floor.py` (Task 3)
- Create `tests/test_analyze_stock_kr_live_price.py` (Task 4)

---

## Task 1: fail-closed floor 헬퍼 (`floor.py`)

**Files:**
- Create: `app/services/symbol_analysis/floor.py`
- Test: `tests/test_symbol_analysis_floor.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_symbol_analysis_floor.py
import pytest

from app.services.symbol_analysis.floor import floored_action, insufficient_inputs


@pytest.mark.unit
def test_insufficient_inputs_lists_missing_core_fields():
    assert insufficient_inputs(
        price_present=True, rsi_present=True, consensus_present=True
    ) == []
    assert insufficient_inputs(
        price_present=True, rsi_present=True, consensus_present=False
    ) == ["consensus"]
    assert insufficient_inputs(
        price_present=False, rsi_present=False, consensus_present=False
    ) == ["price", "rsi14", "consensus"]


@pytest.mark.unit
def test_floored_action_price_absent_is_unavailable():
    assert floored_action("buy", "high", insufficient=["price", "rsi14", "consensus"]) == (
        "unavailable",
        "low",
    )


@pytest.mark.unit
def test_floored_action_insufficient_floors_to_hold():
    assert floored_action("buy", "high", insufficient=["consensus"]) == ("hold", "low")
    assert floored_action("sell", "medium", insufficient=["rsi14"]) == ("hold", "low")


@pytest.mark.unit
def test_floored_action_complete_inputs_passthrough():
    assert floored_action("buy", "high", insufficient=[]) == ("buy", "high")
    assert floored_action("hold", "low", insufficient=[]) == ("hold", "low")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-396 && uv run pytest tests/test_symbol_analysis_floor.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.symbol_analysis.floor`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/symbol_analysis/floor.py
"""fail-closed insufficient-data floor 헬퍼 (ROB-396, ROB-397 정책 재사용).

core 입력(price/rsi14/consensus)이 부족하면 확신적 buy/sell 을 금지한다.
price 부재면 unavailable, 그 외 부족이면 hold 로 내린다.
"""

from __future__ import annotations

# floor 가 검사하는 core 입력 순서 (insufficient_inputs 출력 순서 고정 → 결정적).
_CORE_FIELDS: tuple[str, ...] = ("price", "rsi14", "consensus")


def insufficient_inputs(
    *, price_present: bool, rsi_present: bool, consensus_present: bool
) -> list[str]:
    """부재한 core 입력 이름 리스트 (고정 순서)."""

    present = {
        "price": price_present,
        "rsi14": rsi_present,
        "consensus": consensus_present,
    }
    return [field for field in _CORE_FIELDS if not present[field]]


def floored_action(
    action: str, confidence: str, *, insufficient: list[str]
) -> tuple[str, str]:
    """(action, confidence). price 부재→(unavailable, low); 그 외 부족→(hold, low);
    부족 없으면 입력 그대로 통과."""

    if "price" in insufficient:
        return "unavailable", "low"
    if insufficient:
        return "hold", "low"
    return action, confidence
```

- [ ] **Step 4: Run test to verify it passes + lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-396 && uv run pytest tests/test_symbol_analysis_floor.py -v && uv run ruff check app/services/symbol_analysis/floor.py tests/test_symbol_analysis_floor.py`
Expected: PASS (4 passed); ruff clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-396
git add app/services/symbol_analysis/floor.py tests/test_symbol_analysis_floor.py
git commit -m "feat(ROB-396): fail-closed insufficient-data floor 헬퍼 (397 정책 재사용)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: source 결정성 — pipeline 분기 + dead code 제거

**Files:**
- Modify: `app/mcp_server/tooling/analysis_analyze.py`
- Rewrite: `tests/mcp_server/test_analyze_stock_pipeline_compat.py`

기존 `test_analyze_stock_pipeline_compat.py`는 pipeline 분기 동작을 검증한다(`source == "research_pipeline"`). 분기 제거 후 이 파일을 **결정성 회귀**로 재작성한다: pipeline 플래그가 켜져 있어도 legacy source 가 결정적으로 나오고 판정이 안 뒤집힌다.

- [ ] **Step 1: Write the failing test (재작성)**

`tests/mcp_server/test_analyze_stock_pipeline_compat.py` 전체를 아래로 교체:

```python
# tests/mcp_server/test_analyze_stock_pipeline_compat.py
"""ROB-396: pipeline 플래그가 켜져 있어도 analyze_stock_impl 은 결정적으로
legacy 경로(source)를 사용하며 판정이 호출마다 뒤집히지 않는다."""

from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.mcp_server.tooling.analysis_analyze import analyze_stock_impl


@pytest.fixture
def mock_ohlcv_df():
    return pd.DataFrame(
        {
            "open": [100.0],
            "high": [110.0],
            "low": [90.0],
            "close": [105.0],
            "volume": [1000],
            "value": [105000.0],
        },
        index=[pd.Timestamp.now()],
    )


@pytest.mark.asyncio
async def test_pipeline_flags_do_not_flip_source(mock_ohlcv_df):
    """RESEARCH_PIPELINE 플래그 True 라도 legacy source 로 결정적."""

    with patch("app.mcp_server.tooling.analysis_analyze.settings") as mock_settings:
        mock_settings.RESEARCH_PIPELINE_ANALYZE_STOCK_ENABLED = True
        mock_settings.RESEARCH_PIPELINE_ENABLED = True

        with patch(
            "app.mcp_server.tooling.analysis_analyze._fetch_ohlcv_for_indicators",
            new_callable=AsyncMock,
            return_value=mock_ohlcv_df,
        ), patch(
            "app.mcp_server.tooling.analysis_analyze._get_quote_impl",
            new_callable=AsyncMock,
            return_value={
                "price": 105.0,
                "symbol": "AAPL",
                "instrument_type": "equity_us",
                "source": "yahoo",
            },
        ), patch(
            "app.mcp_server.tooling.analysis_analyze._get_indicators_impl",
            new_callable=AsyncMock,
            return_value={"rsi": {"value": 50}},
        ), patch(
            "app.mcp_server.tooling.analysis_analyze._get_support_resistance_impl",
            new_callable=AsyncMock,
            return_value={},
        ):
            first = await analyze_stock_impl("AAPL")
            second = await analyze_stock_impl("AAPL")

    assert first["source"] == "yahoo"
    assert first["source"] != "research_pipeline"
    # 결정적: 반복 호출에 source/판정 불변
    assert first["source"] == second["source"]
    assert first["recommendation"]["action"] == second["recommendation"]["action"]


@pytest.mark.asyncio
async def test_no_research_pipeline_symbols_in_module():
    """분기 제거 회귀: 모듈에서 pipeline 합성 헬퍼가 사라졌다."""

    import app.mcp_server.tooling.analysis_analyze as mod

    assert not hasattr(mod, "_get_pipeline_result")
    assert not hasattr(mod, "_map_pipeline_to_analysis")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-396 && uv run pytest tests/mcp_server/test_analyze_stock_pipeline_compat.py -v`
Expected: FAIL — `test_no_research_pipeline_symbols_in_module` fails (`_get_pipeline_result` still exists); `test_pipeline_flags_do_not_flip_source` may fail because the live branch still returns `research_pipeline`.

- [ ] **Step 3: Remove the pipeline branch + dead code**

In `app/mcp_server/tooling/analysis_analyze.py`:

1. In `analyze_stock_impl`, delete the entire research-pipeline block (the `if settings.RESEARCH_PIPELINE_ANALYZE_STOCK_ENABLED and settings.RESEARCH_PIPELINE_ENABLED:` ... `# Fall through to legacy path` block, currently ~lines 551-578). After deletion the function flows straight from the `market_type, normalized_symbol = _resolve_market_type(...)` try/except into `analysis = _build_analysis_payload(...)`.

2. Delete the functions `_get_pipeline_result` and `_map_pipeline_to_analysis` (and any private helper used ONLY by them — confirm via grep, e.g. `_map_confidence_score`):

```bash
cd /Users/mgh3326/work/auto_trader.rob-396
grep -rn "_map_confidence_score" app/   # if only inside analysis_analyze.py and only used by _map_pipeline_to_analysis → delete it too
```

3. Remove now-unused imports at the top of the file:
   - `from sqlalchemy import select`
   - `from sqlalchemy.orm import selectinload`
   - `from app.models.research_pipeline import ResearchSession, ResearchSummary`

4. Run ruff to confirm nothing unused remains:

```bash
uv run ruff check app/mcp_server/tooling/analysis_analyze.py
```
If ruff reports any remaining unused import (e.g. another pipeline-only symbol), remove it.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/mgh3326/work/auto_trader.rob-396 && uv run pytest tests/mcp_server/test_analyze_stock_pipeline_compat.py -v && uv run ruff check app/mcp_server/tooling/analysis_analyze.py`
Expected: PASS (2 passed); ruff clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-396
git add app/mcp_server/tooling/analysis_analyze.py tests/mcp_server/test_analyze_stock_pipeline_compat.py
git commit -m "fix(ROB-396): analyze_stock_batch source flip 제거 — pipeline 분기 삭제(legacy 결정성)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: verdict fail-closed floor 배선

**Files:**
- Modify: `app/mcp_server/tooling/analysis_analyze.py` (`_apply_recommendation`)
- Test: `tests/test_analyze_stock_floor.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_analyze_stock_floor.py
import pytest

from app.mcp_server.tooling.analysis_analyze import _apply_recommendation


@pytest.mark.unit
def test_floor_holds_when_consensus_absent():
    # price + rsi 있으나 consensus 없음 → 확신적 buy 금지(hold).
    analysis = {
        "quote": {"price": 1000.0},
        "indicators": {"rsi": {"14": 25.0}},
        "support_resistance": {"supports": [{"price": 950.0}]},
        "opinions": {},  # consensus 없음
        "valuation": {},
    }
    _apply_recommendation(analysis, "equity_kr")
    rec = analysis["recommendation"]
    assert rec["action"] == "hold"
    assert rec["confidence"] == "low"
    assert "consensus" in rec["insufficient_inputs"]


@pytest.mark.unit
def test_floor_unavailable_when_price_absent():
    analysis = {
        "quote": {"price": None},
        "indicators": {},
        "opinions": {},
        "valuation": {},
    }
    _apply_recommendation(analysis, "equity_kr")
    rec = analysis["recommendation"]
    assert rec["action"] == "unavailable"
    assert rec["confidence"] == "low"
    assert "price" in rec["insufficient_inputs"]


@pytest.mark.unit
def test_no_floor_when_inputs_complete():
    # bullish RSI + bullish consensus → buy 통과, insufficient 없음.
    analysis = {
        "quote": {"price": 1000.0},
        "indicators": {"rsi": {"14": 25.0}},
        "support_resistance": {"supports": [{"price": 950.0}]},
        "opinions": {
            "consensus": {
                "buy_count": 8,
                "sell_count": 1,
                "strong_buy_count": 5,
                "total_count": 10,
            }
        },
        "valuation": {},
    }
    _apply_recommendation(analysis, "equity_kr")
    rec = analysis["recommendation"]
    assert rec["action"] == "buy"
    assert rec["insufficient_inputs"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-396 && uv run pytest tests/test_analyze_stock_floor.py -v`
Expected: FAIL — `KeyError: 'insufficient_inputs'` (and `test_floor_unavailable_when_price_absent` fails because current `_apply_recommendation` returns no `recommendation` when price absent).

- [ ] **Step 3: Apply floor in `_apply_recommendation`**

`app/mcp_server/tooling/analysis_analyze.py` 상단 import 에 추가:

```python
from app.services.symbol_analysis.floor import floored_action, insufficient_inputs
```

`_apply_recommendation` 를 아래로 교체:

```python
def _apply_recommendation(
    analysis: dict[str, Any],
    market_type: str,
) -> None:
    if market_type not in ("equity_kr", "equity_us"):
        return

    recommendation = _build_recommendation_for_equity(analysis, market_type)

    quote = analysis.get("quote") or {}
    price_present = quote.get("price") is not None
    consensus_present = bool((analysis.get("opinions") or {}).get("consensus"))

    if recommendation is None:
        # price/quote 부재 → unavailable floor 레코멘데이션을 정직하게 부착.
        recommendation = {
            "action": "hold",
            "confidence": "low",
            "rsi14": None,
            "buy_zones": [],
            "sell_targets": [],
            "stop_loss": None,
            "reasoning": "",
        }
    rsi_present = recommendation.get("rsi14") is not None

    missing = insufficient_inputs(
        price_present=price_present,
        rsi_present=rsi_present,
        consensus_present=consensus_present,
    )
    action, confidence = floored_action(
        recommendation["action"], recommendation["confidence"], insufficient=missing
    )
    recommendation["action"] = action
    recommendation["confidence"] = confidence
    recommendation["insufficient_inputs"] = missing

    analysis["recommendation"] = recommendation
```

Note: equity 에 대해 `recommendation` 이 항상 부착된다(이전엔 price 부재 시 누락). additive 변경 — 소비자는 `unavailable` 을 정직하게 받는다.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/mgh3326/work/auto_trader.rob-396 && uv run pytest tests/test_analyze_stock_floor.py tests/mcp_server/test_analyze_stock_pipeline_compat.py -v && uv run ruff check app/mcp_server/tooling/analysis_analyze.py tests/test_analyze_stock_floor.py`
Expected: PASS; ruff clean. (compat 테스트도 여전히 green — bullish 입력이면 floor 통과.)

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-396
git add app/mcp_server/tooling/analysis_analyze.py tests/test_analyze_stock_floor.py
git commit -m "fix(ROB-396): verdict fail-closed floor — core 입력 부족 시 hold/unavailable + insufficient_inputs

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: 라이브 KR price + 정직 태그

**Files:**
- Modify: `app/mcp_server/tooling/market_data_quotes.py` (`_fetch_kr_live_quote` 추가)
- Modify: `app/mcp_server/tooling/analysis_analyze.py` (`_resolve_kr_quote` 추가 + `_prepare_quote_tasks` KR 배선)
- Test: `tests/test_analyze_stock_kr_live_price.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_analyze_stock_kr_live_price.py
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.mcp_server.tooling import analysis_analyze

KST = ZoneInfo("Asia/Seoul")


def _ohlcv():
    # 전일 일봉(어제 날짜) — fallback 경로용
    yesterday = pd.Timestamp(datetime.now(KST).date() - timedelta(days=1))
    return pd.DataFrame(
        {"open": [100.0], "high": [110.0], "low": [90.0], "close": [105.0],
         "volume": [1000], "value": [105000.0]},
        index=[yesterday],
    )


@pytest.mark.asyncio
async def test_kr_live_price_today_is_not_stale(monkeypatch):
    today = datetime.now(KST)

    async def fake_live(symbol):
        return {
            "symbol": symbol, "instrument_type": "equity_kr",
            "price": 1225000.0, "open": 1200000.0, "high": 1230000.0,
            "low": 1190000.0, "volume": 5, "value": 6,
            "source": "kis", "price_as_of": today.isoformat(),
        }

    monkeypatch.setattr(analysis_analyze, "_fetch_kr_live_quote", fake_live)
    quote = await analysis_analyze._resolve_kr_quote("012450", _ohlcv())
    assert quote["price"] == 1225000.0
    assert quote["is_stale_price"] is False


@pytest.mark.asyncio
async def test_kr_prev_day_quote_is_stale(monkeypatch):
    prev = datetime.now(KST) - timedelta(days=1)

    async def fake_live(symbol):
        return {
            "symbol": symbol, "instrument_type": "equity_kr",
            "price": 1173000.0, "open": 1.0, "high": 1.0, "low": 1.0,
            "volume": 1, "value": 1, "source": "kis",
            "price_as_of": prev.isoformat(),
        }

    monkeypatch.setattr(analysis_analyze, "_fetch_kr_live_quote", fake_live)
    quote = await analysis_analyze._resolve_kr_quote("012450", _ohlcv())
    assert quote["is_stale_price"] is True


@pytest.mark.asyncio
async def test_kr_live_failure_falls_back_to_ohlcv_stale(monkeypatch):
    async def fake_live(symbol):
        return None  # inquire_price 실패/빈응답

    monkeypatch.setattr(analysis_analyze, "_fetch_kr_live_quote", fake_live)
    quote = await analysis_analyze._resolve_kr_quote("012450", _ohlcv())
    assert quote["price"] == 105.0  # 일봉 종가 fallback
    assert quote["is_stale_price"] is True
    assert quote["price_as_of"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-396 && uv run pytest tests/test_analyze_stock_kr_live_price.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_fetch_kr_live_quote'` / `_resolve_kr_quote`.

- [ ] **Step 3: Implement live quote + resolver + wiring**

(a) `app/mcp_server/tooling/market_data_quotes.py` 에 추가 (KISClient 는 이 모듈에 이미 import 됨):

```python
async def _fetch_kr_live_quote(symbol: str) -> dict[str, Any] | None:
    """analyze 전용: KR 라이브 현재가(KIS inquire_price, stck_prpr) + as_of.

    공유 _fetch_quote_equity_kr(orders/portfolio 사용)는 건드리지 않는다.
    실패/빈응답이면 None (호출자가 일봉으로 fallback).
    """
    kis = KISClient()
    try:
        df = await kis.inquire_price(code=symbol, market="J")
    except Exception:
        return None
    if df.empty:
        return None

    row = df.iloc[0].to_dict()  # index=종목코드
    as_of: datetime | None = None
    date_val = row.get("date")
    time_val = row.get("time")
    if date_val is not None:
        d = pd.Timestamp(date_val).to_pydatetime()
        if time_val is not None:
            as_of = datetime.combine(d.date(), time_val)
        else:
            as_of = d

    return {
        "symbol": symbol,
        "instrument_type": "equity_kr",
        "price": row.get("close"),  # stck_prpr → close
        "open": row.get("open"),
        "high": row.get("high"),
        "low": row.get("low"),
        "volume": row.get("volume"),
        "value": row.get("value"),
        "source": "kis",
        "price_as_of": as_of.isoformat() if as_of is not None else None,
    }
```

market_data_quotes.py 상단에 `datetime` import 가 없으면 추가:
```python
from datetime import datetime
```
그리고 `__all__` 가 있으면 `"_fetch_kr_live_quote"` 를 추가한다(다른 export 패턴을 따름).

(b) `app/mcp_server/tooling/analysis_analyze.py` 상단 import 추가:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

from app.mcp_server.tooling.market_data_quotes import _fetch_kr_live_quote
from app.services.symbol_analysis.freshness import compute_is_stale
```

(기존 `market_data_quotes` import 블록에 `_fetch_kr_live_quote` 를 합쳐도 된다.)

analyze 에 resolver 추가 (`_build_kr_quote_from_ohlcv` 정의 근처):

```python
_KST = ZoneInfo("Asia/Seoul")


async def _resolve_kr_quote(
    symbol: str, ohlcv_df: pd.DataFrame
) -> dict[str, Any] | None:
    """KR analyze quote: 라이브 inquire_price 우선, 실패 시 일봉 종가 fallback.
    두 경로 모두 price_as_of + is_stale_price 를 정직하게 태그한다."""
    trading_date = datetime.now(_KST).date()

    live = await _fetch_kr_live_quote(symbol)
    if live is not None:
        as_of_raw = live.get("price_as_of")
        as_of_dt = datetime.fromisoformat(as_of_raw) if as_of_raw else None
        live["is_stale_price"] = compute_is_stale(
            "price", as_of_dt, trading_date=trading_date
        )
        return live

    fallback = _build_kr_quote_from_ohlcv(symbol, ohlcv_df)
    if fallback is None:
        return None
    last_idx = ohlcv_df.index[-1]
    as_of_dt = pd.Timestamp(last_idx).to_pydatetime()
    fallback["price_as_of"] = as_of_dt.isoformat()
    fallback["is_stale_price"] = compute_is_stale(
        "price", as_of_dt, trading_date=trading_date
    )
    return fallback
```

`_prepare_quote_tasks` 의 KR 분기를 라이브 resolver task 로 교체:

```python
    if market_type == "equity_kr":
        named_tasks.append(
            (
                "quote",
                asyncio.create_task(_resolve_kr_quote(normalized_symbol, ohlcv_df)),
            )
        )
        return None, named_tasks
```

(기존 `_build_kr_quote_from_ohlcv` preload + `if preloaded_quote is None` task 분기를 위 한 블록으로 대체. `_build_kr_quote_from_ohlcv` 함수 자체는 `_resolve_kr_quote` 의 fallback 으로 계속 사용되므로 유지.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/mgh3326/work/auto_trader.rob-396 && uv run pytest tests/test_analyze_stock_kr_live_price.py -v && uv run ruff check app/mcp_server/tooling/analysis_analyze.py app/mcp_server/tooling/market_data_quotes.py tests/test_analyze_stock_kr_live_price.py`
Expected: PASS (3 passed); ruff clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-396
git add app/mcp_server/tooling/analysis_analyze.py app/mcp_server/tooling/market_data_quotes.py tests/test_analyze_stock_kr_live_price.py
git commit -m "fix(ROB-396): KR current_price 라이브 inquire_price + price_as_of/is_stale_price 태그

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: 전체 검증

**Files:** (없음 — 검증/회귀만)

- [ ] **Step 1: 신규/변경 테스트 전체 실행**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-396
uv run pytest tests/test_symbol_analysis_floor.py tests/test_analyze_stock_floor.py \
  tests/test_analyze_stock_kr_live_price.py tests/mcp_server/test_analyze_stock_pipeline_compat.py \
  tests/test_symbol_analysis_contract.py tests/test_symbol_analysis_authority.py \
  tests/test_symbol_analysis_freshness.py tests/test_symbol_analysis_derived.py -v
```
Expected: 전부 PASS.

- [ ] **Step 2: 인접 회귀 (analyze/quote 소비자) + import-contract + lint/format**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-396
uv run pytest tests/test_mcp_fundamentals_tools.py tests/test_import_contracts.py -q
uv run ruff check app/ tests/
uv run ruff format --check app/services/symbol_analysis/ app/mcp_server/tooling/analysis_analyze.py app/mcp_server/tooling/market_data_quotes.py tests/test_symbol_analysis_floor.py tests/test_analyze_stock_floor.py tests/test_analyze_stock_kr_live_price.py tests/mcp_server/test_analyze_stock_pipeline_compat.py
```
Expected: PASS; ruff check/format clean. (import-contracts: analyze 가 `app.services.symbol_analysis` 를 import 하는 것은 `mcp_server → services` 허용 방향이므로 위반 아님.)

- [ ] **Step 3: (변경 없으면) 커밋 불필요**

검증만 통과하면 Task 5 는 커밋이 없다. lint/format 수정이 필요하면 해당 파일을 고치고:

```bash
cd /Users/mgh3326/work/auto_trader.rob-396
git add -A && git commit -m "chore(ROB-396): lint/format 정리

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review (작성자 체크)

**Spec coverage:**
- 스펙 §3 source 결정성(pipeline 분기 제거, 전 시장) → Task 2 ✅
- 스펙 §4 verdict fail-closed floor(shape 불변 + insufficient_inputs) → Task 1(헬퍼) + Task 3(배선) ✅
- 스펙 §5 라이브 KR price + price_as_of/is_stale_price + graceful fallback → Task 4 ✅
- 스펙 §6 additive 필드(quote.price_as_of/is_stale_price, recommendation.insufficient_inputs) → Task 3/4 ✅
- 스펙 §7 두 증상 TDD 회귀 → Task 2(증상1) + Task 4(증상2) + Task 3(floor) ✅
- 스펙 §8 비목표(공유 _fetch_quote_equity_kr 불변, US/crypto price 제외) → Task 4 가 analyze 전용 _fetch_kr_live_quote 만 추가, 공유 함수 미변경 ✅

**Placeholder scan:** 모든 step 에 실제 코드/명령/기대 출력 포함. dead-code 제거는 grep 확인 단계를 명시(라인 드리프트 회피). placeholder 없음.

**Type consistency:** `insufficient_inputs`/`floored_action`(Task 1 정의, Task 3 사용) 시그니처 일치. `_fetch_kr_live_quote`(Task 4 정의, 테스트/`_resolve_kr_quote` 사용), `_resolve_kr_quote`(Task 4 정의, `_prepare_quote_tasks` 사용), `compute_is_stale`(ROB-397 기존, `compute_is_stale(category, as_of, *, trading_date)`) 일치. recommendation dict 키(action/confidence/rsi14/buy_zones/sell_targets/stop_loss/reasoning/insufficient_inputs) 일관.

**검증 시 주의:**
- Task 2 dead-code 제거 후 ruff 가 잡는 미사용 import 를 모두 제거(`select`/`selectinload`/`ResearchSession`/`ResearchSummary`, 그리고 `_map_confidence_score` 가 pipeline 전용이면 함께).
- `inquire_price` 반환 DataFrame 컬럼은 date/time/open/high/low/close/volume/value, index=종목코드 (`domestic_market_data.py:175-215` 확인).

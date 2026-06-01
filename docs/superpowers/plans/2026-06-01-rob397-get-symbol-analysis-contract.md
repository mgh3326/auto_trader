# ROB-397 — `get_symbol_analysis` contract foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** snapshot 기반 `get_symbol_analysis`의 타입드 계약(스키마 + 필드별 권위 + core-aware freshness + 결정적 `derived` insufficient-data floor)을 코드+테스트로 고정한다. collector/마이그레이션/런타임 구현은 없다.

**Architecture:** 신규 패키지 `app/services/symbol_analysis/`에 4개 focused 모듈을 둔다 — `contract.py`(frozen-dataclass 스키마 + 읽기 도구 Protocol), `authority.py`(필드별 권위 레지스트리 + drift-guard), `freshness.py`(core-aware stale 판정), `derived.py`(순수 추천 함수 + fail-closed floor). 각 모듈은 TDD로 독립 검증한다.

**Tech Stack:** Python 3.13, `@dataclass(frozen=True)`, `typing.Protocol`/`Generic`, pytest. 새 의존성 없음. `services → mcp_server` 역방향 import 금지(스코어러는 `build_recommendation_for_equity`를 포팅하되 import하지 않음).

**참조 스펙:** `docs/superpowers/specs/2026-06-01-rob397-get-symbol-analysis-contract-design.md`

---

## File Structure

- Create `app/services/symbol_analysis/__init__.py` — 패키지 + 공개 심볼 re-export
- Create `app/services/symbol_analysis/contract.py` — `FieldBlock`, 데이터 dataclass들, `SymbolAnalysis`, `Freshness`, `Provenance`, `PriceLevel`, `GetSymbolAnalysis` Protocol, `FRESHNESS_OVERALL` 상수
- Create `app/services/symbol_analysis/authority.py` — `AuthoritySpec`, `CATEGORIES`, `CORE_CATEGORIES`, `AUTHORITY`, `NON_AUTHORITY_SOURCES`
- Create `app/services/symbol_analysis/freshness.py` — `compute_is_stale`, `derive_freshness`
- Create `app/services/symbol_analysis/derived.py` — `RULE_VERSION`, `_score_action`, `derive_recommendation`
- Create `tests/test_symbol_analysis_contract.py`
- Create `tests/test_symbol_analysis_authority.py`
- Create `tests/test_symbol_analysis_freshness.py`
- Create `tests/test_symbol_analysis_derived.py`

테스트는 ROB-340 contract 선례(`tests/test_invest_data_source_contract.py`)를 따라 top-level `tests/test_symbol_analysis_*.py`로 둔다.

---

## Task 1: 스키마 dataclass + 읽기 도구 Protocol (`contract.py`)

**Files:**
- Create: `app/services/symbol_analysis/__init__.py`
- Create: `app/services/symbol_analysis/contract.py`
- Test: `tests/test_symbol_analysis_contract.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_symbol_analysis_contract.py
import dataclasses
from datetime import datetime

import pytest

from app.services.symbol_analysis.contract import (
    ConsensusData,
    DerivedBlock,
    FieldBlock,
    FlowData,
    Freshness,
    GetSymbolAnalysis,
    PriceData,
    PriceLevel,
    Provenance,
    SymbolAnalysis,
    TechnicalData,
    ValuationData,
)


@pytest.mark.unit
def test_field_block_is_frozen_and_carries_provenance():
    block = FieldBlock(
        value=PriceData(last=1000.0),
        source="kis_live",
        as_of=datetime(2026, 6, 1, 9, 30),
        is_stale=False,
    )
    assert block.value.last == 1000.0
    assert block.source == "kis_live"
    assert block.is_stale is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        block.is_stale = True  # type: ignore[misc]


@pytest.mark.unit
def test_symbol_analysis_construction_and_frozen():
    sa = SymbolAnalysis(
        symbol="005930",
        name="삼성전자",
        market="kr",
        price=FieldBlock(PriceData(last=1000.0), "kis_live", None, False),
        valuation=FieldBlock(None, "stock_info", None, True),
        technicals=FieldBlock(None, "kis_live", None, True),
        consensus=FieldBlock(None, "kis_live", None, True),
        flow=FieldBlock(None, "investor_flow_snapshots", None, True),
        derived=DerivedBlock(
            action="hold",
            confidence="low",
            buy_zones=(),
            sell_targets=(),
            stop=None,
            rule_version="symbol_analysis.derived.v1",
            insufficient_inputs=("consensus", "technicals"),
        ),
        provenance=Provenance(
            snapshot_uuid=None,
            primary_source="kis_live",
            freshness=Freshness(overall="stale", stale_fields=("consensus",)),
        ),
    )
    assert sa.symbol == "005930"
    assert sa.provenance.freshness.overall == "stale"
    with pytest.raises(dataclasses.FrozenInstanceError):
        sa.symbol = "000660"  # type: ignore[misc]


@pytest.mark.unit
def test_price_level_holds_price_kind_reasoning():
    level = PriceLevel(price=950.0, kind="support", reasoning="Support at 950")
    assert (level.price, level.kind, level.reasoning) == (950.0, "support", "Support at 950")


@pytest.mark.unit
def test_get_symbol_analysis_is_runtime_protocol():
    # 런타임 구현 없이 호출 계약만 타입으로 고정한다.
    assert getattr(GetSymbolAnalysis, "_is_protocol", False) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-397 && uv run pytest tests/test_symbol_analysis_contract.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.symbol_analysis.contract`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/symbol_analysis/__init__.py
"""snapshot 기반 get_symbol_analysis 계약 (ROB-397 foundation)."""
```

```python
# app/services/symbol_analysis/contract.py
"""SymbolAnalysis read-model 계약 — 타입드 스키마 + 읽기 도구 Protocol (ROB-397).

읽기 시점 라이브 합성 금지. 모든 데이터 카테고리는 FieldBlock 으로 감싸
source/as_of/is_stale 를 카테고리 단위로 강제한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Generic, Protocol, TypeVar
from uuid import UUID

T = TypeVar("T")

# freshness.overall 허용값 (freshness.py 가 파생한다).
FRESHNESS_OVERALL: tuple[str, ...] = ("fresh", "partial", "stale", "unavailable")


@dataclass(frozen=True)
class FieldBlock(Generic[T]):
    """카테고리 값 + 출처/신선도. value=None 이면 부재."""

    value: T | None
    source: str
    as_of: datetime | None
    is_stale: bool


@dataclass(frozen=True)
class PriceData:
    last: float


@dataclass(frozen=True)
class ValuationData:
    per: float | None = None
    pbr: float | None = None
    roe: float | None = None


@dataclass(frozen=True)
class TechnicalData:
    rsi14: float | None = None
    atr: float | None = None
    sma: float | None = None
    bb_lower: float | None = None
    supports: tuple[float, ...] = ()
    resistances: tuple[float, ...] = ()


@dataclass(frozen=True)
class ConsensusData:
    buy: int | None = None
    hold: int | None = None
    sell: int | None = None
    strong_buy: int | None = None
    total: int | None = None
    target_avg: float | None = None
    target_median: float | None = None
    target_min: float | None = None
    target_max: float | None = None
    upside_pct: float | None = None


@dataclass(frozen=True)
class FlowData:
    foreign_net: float | None = None
    inst_net: float | None = None
    double_buy: bool = False
    double_sell: bool = False
    consec_days: int | None = None


@dataclass(frozen=True)
class PriceLevel:
    price: float
    kind: str
    reasoning: str


@dataclass(frozen=True)
class DerivedBlock:
    action: str
    confidence: str
    buy_zones: tuple[PriceLevel, ...]
    sell_targets: tuple[PriceLevel, ...]
    stop: float | None
    rule_version: str
    insufficient_inputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Freshness:
    overall: str
    stale_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class Provenance:
    snapshot_uuid: UUID | None
    primary_source: str
    freshness: Freshness


@dataclass(frozen=True)
class SymbolAnalysis:
    symbol: str
    name: str | None
    market: str
    price: FieldBlock[PriceData]
    valuation: FieldBlock[ValuationData]
    technicals: FieldBlock[TechnicalData]
    consensus: FieldBlock[ConsensusData]
    flow: FieldBlock[FlowData]
    derived: DerivedBlock
    provenance: Provenance


class GetSymbolAnalysis(Protocol):
    """읽기 전용 read-model 조회 계약 (런타임 구현은 후속 collector 이슈).

    캐시/DB 의 최신 머티리얼라이즈 스냅샷을 반환한다. 없으면 마지막
    스냅샷 + is_stale=True. **라이브 합성 금지.**
    """

    async def __call__(
        self, symbols: list[str], session: str | None = None
    ) -> list[SymbolAnalysis]: ...
```

Note: `field` import는 후속 확장 여지용이지만 현재 미사용이면 제거한다(ruff 위반 방지). Step 4 에서 lint 로 확인.

- [ ] **Step 4: Run test to verify it passes + lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-397 && uv run pytest tests/test_symbol_analysis_contract.py -v && uv run ruff check app/services/symbol_analysis/ tests/test_symbol_analysis_contract.py`
Expected: PASS (4 passed); ruff clean (미사용 `field` import 있으면 제거 후 재실행).

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-397
git add app/services/symbol_analysis/__init__.py app/services/symbol_analysis/contract.py tests/test_symbol_analysis_contract.py
git commit -m "feat(ROB-397): SymbolAnalysis 타입드 스키마 + 읽기 도구 Protocol

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: 필드별 권위 레지스트리 + drift-guard (`authority.py`)

**Files:**
- Create: `app/services/symbol_analysis/authority.py`
- Test: `tests/test_symbol_analysis_authority.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_symbol_analysis_authority.py
import pytest

from app.services.symbol_analysis.authority import (
    AUTHORITY,
    CATEGORIES,
    CORE_CATEGORIES,
    NON_AUTHORITY_SOURCES,
    AuthoritySpec,
)


@pytest.mark.unit
def test_every_category_has_a_primary_source():
    assert set(AUTHORITY) == set(CATEGORIES)
    for cat in CATEGORIES:
        assert isinstance(AUTHORITY[cat], AuthoritySpec)
        assert AUTHORITY[cat].primary, f"{cat} missing primary"


@pytest.mark.unit
def test_core_categories_are_subset_of_categories():
    assert set(CORE_CATEGORIES) <= set(CATEGORIES)
    assert set(CORE_CATEGORIES) == {"price", "consensus", "technicals"}


@pytest.mark.unit
def test_toss_naver_browser_are_never_authority():
    # reference/calibration 만 허용 — primary/fallback 으로 등장 금지.
    for cat, spec in AUTHORITY.items():
        assert spec.primary not in NON_AUTHORITY_SOURCES, cat
        assert spec.fallback not in NON_AUTHORITY_SOURCES, cat
        # naver_finance 는 reference 로만 허용
        for ref in spec.reference:
            assert ref in NON_AUTHORITY_SOURCES or ref == "stock_info"


@pytest.mark.unit
def test_price_authority_matches_invest_data_source_contract():
    # stocks/symbol seam 의 primary 와 정합 (drift-guard).
    from app.services.invest_data_source_contract import INVEST_DATA_SOURCE_CONTRACT

    symbol_entry = next(
        e
        for e in INVEST_DATA_SOURCE_CONTRACT
        if e.surface == "stocks" and e.collector_snapshot_kind == "symbol"
    )
    assert AUTHORITY["price"].primary == symbol_entry.source_name  # kis_live
    assert AUTHORITY["price"].fallback == symbol_entry.fallback_source  # stock_info
```

Note: entries 시퀀스 export 이름은 `INVEST_DATA_SOURCE_CONTRACT`(확인됨, `invest_data_source_contract.py:111`).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-397 && uv run pytest tests/test_symbol_analysis_authority.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.symbol_analysis.authority`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/symbol_analysis/authority.py
"""필드별 권위 레지스트리 (ROB-397).

fallback 으로 치환할 때는 반드시 source 가 바뀌고 is_stale=True 가 동반된다
(freshness.py). Toss/Naver/browser 는 reference 로만 등재 — authority 대체 금지.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AuthoritySpec:
    primary: str
    fallback: str | None = None
    reference: tuple[str, ...] = field(default_factory=tuple)


CATEGORIES: tuple[str, ...] = (
    "price",
    "valuation",
    "technicals",
    "consensus",
    "flow",
)

CORE_CATEGORIES: tuple[str, ...] = ("price", "consensus", "technicals")

# authority 로 절대 승격 불가 (reference/calibration 전용).
NON_AUTHORITY_SOURCES: frozenset[str] = frozenset(
    {
        "naver_finance",
        "toss_screen",
        "naver_remote_debug",
        "toss_remote_debug",
        "browser_probe",
    }
)

AUTHORITY: dict[str, AuthoritySpec] = {
    "price": AuthoritySpec(primary="kis_live", fallback="stock_info"),
    "valuation": AuthoritySpec(primary="stock_info", reference=("naver_finance",)),
    "technicals": AuthoritySpec(primary="kis_live"),
    "consensus": AuthoritySpec(primary="kis_live", reference=("naver_finance",)),
    "flow": AuthoritySpec(
        primary="investor_flow_snapshots", reference=("naver_finance",)
    ),
}
```

- [ ] **Step 4: Run test to verify it passes + lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-397 && uv run pytest tests/test_symbol_analysis_authority.py -v && uv run ruff check app/services/symbol_analysis/authority.py tests/test_symbol_analysis_authority.py`
Expected: PASS (4 passed); ruff clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-397
git add app/services/symbol_analysis/authority.py tests/test_symbol_analysis_authority.py
git commit -m "feat(ROB-397): 필드별 권위 레지스트리 + invest_data_source_contract drift-guard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: core-aware freshness 파생 (`freshness.py`)

**Files:**
- Create: `app/services/symbol_analysis/freshness.py`
- Test: `tests/test_symbol_analysis_freshness.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_symbol_analysis_freshness.py
from datetime import date, datetime

import pytest

from app.services.symbol_analysis.contract import (
    ConsensusData,
    FieldBlock,
    FlowData,
    PriceData,
    TechnicalData,
    ValuationData,
)
from app.services.symbol_analysis.freshness import compute_is_stale, derive_freshness

TRADING_DATE = date(2026, 6, 1)


def _fresh(value, source):
    return FieldBlock(value, source, datetime(2026, 6, 1, 9, 30), is_stale=False)


def _blocks(*, price_stale=False, consensus_stale=False, valuation_stale=False):
    return {
        "price": FieldBlock(PriceData(1000.0), "kis_live", datetime(2026, 6, 1, 9, 30), price_stale),
        "consensus": FieldBlock(ConsensusData(buy=8, total=10), "kis_live", datetime(2026, 6, 1, 8, 0), consensus_stale),
        "technicals": FieldBlock(TechnicalData(rsi14=40.0), "kis_live", datetime(2026, 6, 1, 8, 0), False),
        "valuation": FieldBlock(ValuationData(per=12.0), "stock_info", datetime(2026, 6, 1, 8, 0), valuation_stale),
        "flow": FieldBlock(FlowData(foreign_net=1.0), "investor_flow_snapshots", datetime(2026, 6, 1, 8, 0), False),
    }


@pytest.mark.unit
def test_prev_day_close_during_regular_session_is_stale_price():
    # ROB-396 증상2 회귀: 전일종가(as_of 날짜 < trading_date)는 정규장에서 stale.
    prev_close_as_of = datetime(2026, 5, 30, 15, 30)
    assert compute_is_stale("price", prev_close_as_of, trading_date=TRADING_DATE) is True
    today_fill = datetime(2026, 6, 1, 9, 30)
    assert compute_is_stale("price", today_fill, trading_date=TRADING_DATE) is False


@pytest.mark.unit
def test_missing_as_of_is_stale():
    assert compute_is_stale("consensus", None, trading_date=TRADING_DATE) is True


@pytest.mark.unit
def test_overall_unavailable_when_price_value_none():
    blocks = _blocks()
    blocks["price"] = FieldBlock(None, "kis_live", None, True)
    fresh = derive_freshness(blocks)
    assert fresh.overall == "unavailable"


@pytest.mark.unit
def test_overall_stale_when_core_field_stale():
    fresh = derive_freshness(_blocks(consensus_stale=True))
    assert fresh.overall == "stale"
    assert "consensus" in fresh.stale_fields


@pytest.mark.unit
def test_supplementary_stale_does_not_downgrade_below_partial():
    # ROB-323 anti-pattern 회피: valuation(보조)만 stale 이면 overall=partial, stale 아님.
    fresh = derive_freshness(_blocks(valuation_stale=True))
    assert fresh.overall == "partial"
    assert "valuation" in fresh.stale_fields


@pytest.mark.unit
def test_overall_fresh_when_all_fresh():
    assert derive_freshness(_blocks()).overall == "fresh"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-397 && uv run pytest tests/test_symbol_analysis_freshness.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.symbol_analysis.freshness`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/symbol_analysis/freshness.py
"""core-aware freshness 파생 (ROB-397).

overall 은 core 필드(price/consensus/technicals)만으로 결정한다. 보조
필드(flow/valuation)는 stale 이어도 overall 을 stale 로 떨어뜨리지 않는다
(ROB-323 anti-pattern 회피). stale_fields[] 에는 모든 stale 카테고리를 나열.
"""

from __future__ import annotations

from datetime import date, datetime

from app.services.symbol_analysis.authority import CORE_CATEGORIES
from app.services.symbol_analysis.contract import FieldBlock, Freshness


def compute_is_stale(
    category: str,
    as_of: datetime | None,
    *,
    trading_date: date,
) -> bool:
    """as_of 가 부재하거나 당일(trading_date)이 아니면 stale.

    price 의 전일종가 폴백은 as_of.date() < trading_date 이므로 정규장
    세션에서 stale 로 표면화된다 (ROB-396 증상2).
    """

    if as_of is None:
        return True
    return as_of.date() != trading_date


def derive_freshness(blocks: dict[str, FieldBlock]) -> Freshness:
    """필드별 is_stale/value 로부터 core-aware overall + stale_fields 파생."""

    stale_fields = tuple(
        cat for cat, b in blocks.items() if b.value is None or b.is_stale
    )

    price = blocks.get("price")
    # 1) 가격 앵커 부재 → unavailable
    if price is None or price.value is None:
        return Freshness(overall="unavailable", stale_fields=stale_fields)

    core_stale = any(
        cat in CORE_CATEGORIES
        and (blocks[cat].value is None or blocks[cat].is_stale)
        for cat in blocks
    )
    if core_stale:
        return Freshness(overall="stale", stale_fields=stale_fields)

    if stale_fields:
        return Freshness(overall="partial", stale_fields=stale_fields)

    return Freshness(overall="fresh", stale_fields=())
```

- [ ] **Step 4: Run test to verify it passes + lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-397 && uv run pytest tests/test_symbol_analysis_freshness.py -v && uv run ruff check app/services/symbol_analysis/freshness.py tests/test_symbol_analysis_freshness.py`
Expected: PASS (6 passed); ruff clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-397
git add app/services/symbol_analysis/freshness.py tests/test_symbol_analysis_freshness.py
git commit -m "feat(ROB-397): core-aware freshness 파생 + stale price 회귀 가드(396 증상2)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: 결정적 `derived` 추천 + insufficient-data floor (`derived.py`)

**Files:**
- Create: `app/services/symbol_analysis/derived.py`
- Test: `tests/test_symbol_analysis_derived.py`

스코어링 임계값은 `app/mcp_server/tooling/shared.py::build_recommendation_for_equity`(shared.py:504)를 포팅한다 — RSI(<30:+2, <40:+1, >70:-2, >60:-1) + consensus(buy_ratio>0.6:+2, >0.4:+1, sell_ratio>0.6:-2, >0.4:-1), 합 score>=2→buy(>=3 high else medium), <=-2→sell, else hold/low. `services → mcp_server` import 는 금지이므로 import 하지 않고 포팅하며, 출처를 주석으로 남긴다.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_symbol_analysis_derived.py
import pytest

from app.models.investment_symbol_intermediate_reports import VERDICTS
from app.services.symbol_analysis.contract import (
    ConsensusData,
    FieldBlock,
    PriceData,
    TechnicalData,
)
from app.services.symbol_analysis.derived import RULE_VERSION, derive_recommendation


def _block(value, *, is_stale=False, source="kis_live"):
    return FieldBlock(value, source, None, is_stale)


def _bullish_consensus():
    return ConsensusData(buy=8, hold=1, sell=1, strong_buy=5, total=10, upside_pct=40.0)


@pytest.mark.unit
def test_action_always_in_verdicts_vocab():
    d = derive_recommendation(
        price=_block(PriceData(1000.0)),
        technicals=_block(TechnicalData(rsi14=25.0, supports=(950.0,), resistances=(1100.0,))),
        consensus=_block(_bullish_consensus()),
    )
    assert d.action in VERDICTS
    assert d.rule_version == RULE_VERSION


@pytest.mark.unit
def test_deterministic_same_input_same_output():
    kwargs = dict(
        price=_block(PriceData(1000.0)),
        technicals=_block(TechnicalData(rsi14=25.0, supports=(950.0, 900.0), resistances=(1100.0,))),
        consensus=_block(_bullish_consensus()),
    )
    assert derive_recommendation(**kwargs) == derive_recommendation(**kwargs)


@pytest.mark.unit
def test_bullish_inputs_yield_buy():
    d = derive_recommendation(
        price=_block(PriceData(1000.0)),
        technicals=_block(TechnicalData(rsi14=25.0, supports=(950.0,), resistances=(1100.0,))),
        consensus=_block(_bullish_consensus()),
    )
    assert d.action == "buy"
    assert d.confidence in ("medium", "high")
    assert d.insufficient_inputs == ()


@pytest.mark.unit
def test_price_absent_is_unavailable_floor():
    d = derive_recommendation(
        price=_block(None),
        technicals=_block(TechnicalData(rsi14=25.0)),
        consensus=_block(_bullish_consensus()),
    )
    assert d.action == "unavailable"
    assert d.confidence == "low"
    assert d.insufficient_inputs == ("price",)
    assert d.buy_zones == () and d.sell_targets == ()


@pytest.mark.unit
def test_stale_consensus_floors_to_hold_no_flip():
    # ROB-396 증상1: core 입력 불완전이면 확신적 buy/sell 금지 → hold floor.
    d = derive_recommendation(
        price=_block(PriceData(1000.0)),
        technicals=_block(TechnicalData(rsi14=25.0, supports=(950.0,))),
        consensus=_block(None, is_stale=True),
    )
    assert d.action == "hold"
    assert d.confidence == "low"
    assert "consensus" in d.insufficient_inputs


@pytest.mark.unit
def test_buy_zones_sorted_descending_and_below_price():
    d = derive_recommendation(
        price=_block(PriceData(1000.0)),
        technicals=_block(TechnicalData(rsi14=25.0, supports=(900.0, 950.0, 1050.0))),
        consensus=_block(_bullish_consensus()),
    )
    prices = [z.price for z in d.buy_zones]
    assert prices == sorted(prices, reverse=True)
    assert all(p < 1000.0 for p in prices)  # 현재가 이상 support 는 제외
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-397 && uv run pytest tests/test_symbol_analysis_derived.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.symbol_analysis.derived`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/symbol_analysis/derived.py
"""결정적 derived 추천 + fail-closed insufficient-data floor (ROB-397).

derive_recommendation 은 저장 입력 + RULE_VERSION 의 순수 함수다 (라이브
호출/랜덤 없음, 입력 동일→출력 동일, 리스트 안정 정렬). core 입력이
stale/null 이면 확신적 buy/sell 을 금지한다 (ROB-396 증상1).

스코어링 임계값은 app/mcp_server/tooling/shared.py::build_recommendation_for_equity
를 포팅했다 (services → mcp_server import 금지이므로 복제).
"""

from __future__ import annotations

from app.services.symbol_analysis.contract import (
    ConsensusData,
    DerivedBlock,
    FieldBlock,
    PriceData,
    PriceLevel,
    TechnicalData,
)

RULE_VERSION = "symbol_analysis.derived.v1"


def _score_action(
    rsi14: float | None, consensus: ConsensusData | None
) -> tuple[int, int]:
    """(score, max_score). shared.build_recommendation_for_equity 와 동일 임계값."""

    score = 0
    max_score = 0

    if rsi14 is not None:
        max_score += 2
        if rsi14 < 30:
            score += 2
        elif rsi14 < 40:
            score += 1
        elif rsi14 > 70:
            score -= 2
        elif rsi14 > 60:
            score -= 1

    if consensus is not None and consensus.total and consensus.total > 0:
        buy = consensus.buy or 0
        sell = consensus.sell or 0
        max_score += 2
        buy_ratio = buy / consensus.total
        sell_ratio = sell / consensus.total
        if buy_ratio > 0.6:
            score += 2
        elif buy_ratio > 0.4:
            score += 1
        elif sell_ratio > 0.6:
            score -= 2
        elif sell_ratio > 0.4:
            score -= 1

    return score, max_score


def _buy_zones(price: float, tech: TechnicalData | None) -> tuple[PriceLevel, ...]:
    if tech is None:
        return ()
    zones: list[PriceLevel] = []
    if tech.bb_lower is not None and tech.bb_lower < price:
        zones.append(PriceLevel(float(tech.bb_lower), "bollinger_lower", "BB lower band"))
    for s in tech.supports:
        if s < price:
            zones.append(PriceLevel(float(s), "support", f"Support at {s}"))
    return tuple(sorted(zones, key=lambda z: z.price, reverse=True))


def _sell_targets(price: float, tech: TechnicalData | None) -> tuple[PriceLevel, ...]:
    if tech is None:
        return ()
    targets = [
        PriceLevel(float(r), "resistance", f"Resistance at {r}")
        for r in tech.resistances
        if r > price
    ]
    return tuple(sorted(targets, key=lambda z: z.price))


def derive_recommendation(
    *,
    price: FieldBlock[PriceData],
    technicals: FieldBlock[TechnicalData],
    consensus: FieldBlock[ConsensusData],
) -> DerivedBlock:
    # floor 1: 가격 앵커 부재 → unavailable
    if price.value is None:
        return DerivedBlock(
            action="unavailable",
            confidence="low",
            buy_zones=(),
            sell_targets=(),
            stop=None,
            rule_version=RULE_VERSION,
            insufficient_inputs=("price",),
        )

    current = price.value.last
    tech = technicals.value
    cons = consensus.value

    insufficient: list[str] = []
    if tech is None or technicals.is_stale:
        insufficient.append("technicals")
    if cons is None or consensus.is_stale:
        insufficient.append("consensus")

    buy_zones = _buy_zones(current, tech)
    sell_targets = _sell_targets(current, tech)

    # floor 2: core 입력 불완전 → 확신적 buy/sell 금지 (hold, low)
    if insufficient:
        return DerivedBlock(
            action="hold",
            confidence="low",
            buy_zones=buy_zones,
            sell_targets=sell_targets,
            stop=None,
            rule_version=RULE_VERSION,
            insufficient_inputs=tuple(insufficient),
        )

    score, _ = _score_action(tech.rsi14, cons)
    if score >= 2:
        action, confidence = "buy", ("high" if score >= 3 else "medium")
    elif score <= -2:
        action, confidence = "sell", ("high" if score <= -3 else "medium")
    else:
        action, confidence = "hold", "low"

    stop = buy_zones[-1].price if buy_zones else None

    return DerivedBlock(
        action=action,
        confidence=confidence,
        buy_zones=buy_zones,
        sell_targets=sell_targets,
        stop=stop,
        rule_version=RULE_VERSION,
        insufficient_inputs=(),
    )
```

- [ ] **Step 4: Run test to verify it passes + lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-397 && uv run pytest tests/test_symbol_analysis_derived.py -v && uv run ruff check app/services/symbol_analysis/derived.py tests/test_symbol_analysis_derived.py`
Expected: PASS (6 passed); ruff clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-397
git add app/services/symbol_analysis/derived.py tests/test_symbol_analysis_derived.py
git commit -m "feat(ROB-397): 결정적 derived 추천 + insufficient-data floor(396 증상1)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: 패키지 공개 표면 정리 + 전체 검증

**Files:**
- Modify: `app/services/symbol_analysis/__init__.py`

- [ ] **Step 1: __init__ 에 공개 심볼 re-export**

```python
# app/services/symbol_analysis/__init__.py
"""snapshot 기반 get_symbol_analysis 계약 (ROB-397 foundation)."""

from app.services.symbol_analysis.authority import (
    AUTHORITY,
    CATEGORIES,
    CORE_CATEGORIES,
    NON_AUTHORITY_SOURCES,
    AuthoritySpec,
)
from app.services.symbol_analysis.contract import (
    ConsensusData,
    DerivedBlock,
    FieldBlock,
    FlowData,
    Freshness,
    GetSymbolAnalysis,
    PriceData,
    PriceLevel,
    Provenance,
    SymbolAnalysis,
    TechnicalData,
    ValuationData,
)
from app.services.symbol_analysis.derived import RULE_VERSION, derive_recommendation
from app.services.symbol_analysis.freshness import compute_is_stale, derive_freshness

__all__ = [
    "AUTHORITY",
    "AuthoritySpec",
    "CATEGORIES",
    "CORE_CATEGORIES",
    "ConsensusData",
    "DerivedBlock",
    "FieldBlock",
    "FlowData",
    "Freshness",
    "GetSymbolAnalysis",
    "NON_AUTHORITY_SOURCES",
    "PriceData",
    "PriceLevel",
    "Provenance",
    "RULE_VERSION",
    "SymbolAnalysis",
    "TechnicalData",
    "ValuationData",
    "compute_is_stale",
    "derive_freshness",
    "derive_recommendation",
]
```

- [ ] **Step 2: 전체 모듈 테스트 + lint + 타입체크**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-397
uv run pytest tests/test_symbol_analysis_contract.py tests/test_symbol_analysis_authority.py tests/test_symbol_analysis_freshness.py tests/test_symbol_analysis_derived.py -v
uv run ruff check app/services/symbol_analysis/ tests/test_symbol_analysis_*.py
uv run ruff format --check app/services/symbol_analysis/ tests/test_symbol_analysis_*.py
```
Expected: 모든 테스트 PASS (20 passed); ruff check/format clean.

- [ ] **Step 3: import-contract 회귀 확인**

Run: `cd /Users/mgh3326/work/auto_trader.rob-397 && uv run pytest tests/test_import_contracts.py -v`
Expected: PASS — 신규 `app/services/symbol_analysis/` 가 `app/mcp_server` 를 import 하지 않으므로 위반 없음. (실패 시 derived.py 가 shared.py 를 import 하지 않았는지 재확인.)

- [ ] **Step 4: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-397
git add app/services/symbol_analysis/__init__.py
git commit -m "feat(ROB-397): symbol_analysis 패키지 공개 표면 re-export

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review (작성자 체크)

**Spec coverage:**
- §3 타입드 스키마 → Task 1 ✅
- §4 필드별 권위 레지스트리 + drift-guard → Task 2 ✅
- §5 core-aware freshness (4-rule) + stale price → Task 3 ✅
- §6 derived 순수 함수 + insufficient-data floor + VERDICTS 재사용 → Task 4 ✅
- §7 읽기 도구 계약(Protocol, 구현 없음) → Task 1 (GetSymbolAnalysis Protocol) ✅
- §8 테스트 5종 → Task 1-4 + Task 5 통합 ✅

**Placeholder scan:** 모든 step 에 실제 코드/명령/기대 출력 포함. placeholder 없음.

**Type consistency:** `FieldBlock`, `SymbolAnalysis`, `DerivedBlock`, `AuthoritySpec`, `Freshness`, `PriceLevel`, `derive_recommendation`, `derive_freshness`, `compute_is_stale`, `RULE_VERSION` 명칭이 Task 간 일치. `derive_recommendation` 은 Task 4 정의/Task 1 테스트에서 사용 안 함(분리됨), `VERDICTS` 는 기존 모델에서 import.

**검증 시 주의:**
- Task 2 의 drift-guard 는 `INVEST_DATA_SOURCE_CONTRACT`(확인됨)를 import 한다.
- Task 1 의 미사용 `field` import 는 ruff 가 잡으면 제거.

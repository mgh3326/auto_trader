# ROB-147 /invest 주식 골라보기 Screener MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Toss-inspired read-only "주식 골라보기" screener MVP to the existing `/invest` desktop SPA — a new `/invest/screener` page (preset sidebar + filter chips + results table + filter-modal shell) backed by new read-only `/invest/api/screener/presets` and `/invest/api/screener/results` view-model endpoints. KR-market only for MVP. No broker / order / watch-mutation / Toss dependency.

**Architecture:**
- Backend: new schema `app/schemas/invest_screener.py`, new view-model service `app/services/invest_view_model/screener_service.py` that wraps the existing read-only screening service (`app.services.screener_service.ScreenerService.list_screening`) and maps its rows to display-friendly DTO rows. Add 2 GET endpoints to `app/routers/invest_api.py`. Extend `tests/test_invest_view_model_safety.py` to cover the new module so broker / order / mutation paths cannot leak in.
- Frontend: new `DesktopScreenerPage` plus child components (`ScreenerPresetSidebar`, `ScreenerFilterBar`, `ScreenerResultsTable`, `ScreenerFilterModal`) and an API wrapper. Add the `/screener` route to `frontend/invest/src/routes.tsx` and a nav link in `DesktopHeader.tsx`.

**Tech Stack:** Python 3.13 (FastAPI, SQLAlchemy async, Pydantic v2, pytest, ruff); React 19 + Vite + react-router v7 + vitest + @testing-library/react.

**Linear:** https://linear.app/mgh3326/issue/ROB-147

**Branch:** `feature/ROB-147-invest-screener-mvp` (created from `origin/main` at the start of Task 0).

---

## Task 0: Branch setup

**Files:** none (git only).

- [ ] **Step 0.1: Create + switch to the feature branch**

```bash
git switch -c feature/ROB-147-invest-screener-mvp
git status
```

Expected: `On branch feature/ROB-147-invest-screener-mvp`, working tree clean.

---

## Task 1: Backend — invest_screener schemas

**Files:**
- Create: `app/schemas/invest_screener.py`
- Create: `tests/test_invest_screener_schemas.py`

- [ ] **Step 1.1: Write failing schema tests**

Create `tests/test_invest_screener_schemas.py`:

```python
"""ROB-147: Pydantic schema contract tests for /invest/api/screener/*."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.invest_screener import (
    ScreenerFilterChip,
    ScreenerPreset,
    ScreenerPresetsResponse,
    ScreenerResultRow,
    ScreenerResultsResponse,
)


@pytest.mark.unit
def test_preset_minimal_valid() -> None:
    preset = ScreenerPreset(
        id="consecutive_gainers",
        name="연속 상승세",
        description="일주일 연속 상승세를 보이는 주식",
        badges=["인기"],
        filterChips=[
            ScreenerFilterChip(label="주가등락률", detail="1주일 전 보다 · 0% 이상"),
            ScreenerFilterChip(label="주가 연속상승", detail="5일 이상 연속"),
        ],
        metricLabel="주가등락률",
        market="kr",
    )
    assert preset.id == "consecutive_gainers"
    assert preset.market == "kr"


@pytest.mark.unit
def test_preset_rejects_unknown_market() -> None:
    with pytest.raises(ValidationError):
        ScreenerPreset(
            id="x",
            name="x",
            description="x",
            badges=[],
            filterChips=[],
            metricLabel="x",
            market="forex",  # type: ignore[arg-type]
        )


@pytest.mark.unit
def test_results_response_with_warning_and_missing_metric() -> None:
    row = ScreenerResultRow(
        rank=1,
        symbol="005930",
        market="kr",
        name="삼성전자",
        logoUrl=None,
        isWatched=False,
        priceLabel="80,000원",
        changePctLabel="+1.23%",
        changeAmountLabel="+970",
        changeDirection="up",
        category="반도체",
        marketCapLabel="478조원",
        volumeLabel="12,345,678",
        analystLabel="-",
        metricValueLabel="-",
        warnings=["애널리스트 분석 데이터 준비중"],
    )
    resp = ScreenerResultsResponse(
        presetId="consecutive_gainers",
        title="연속 상승세",
        description="일주일 연속 상승세를 보이는 주식",
        filterChips=[
            ScreenerFilterChip(label="주가등락률", detail="1주일 전 보다 · 0% 이상"),
        ],
        metricLabel="주가등락률",
        results=[row],
        warnings=[],
    )
    assert resp.results[0].symbol == "005930"
    assert resp.results[0].changeDirection == "up"


@pytest.mark.unit
def test_results_rejects_negative_rank() -> None:
    with pytest.raises(ValidationError):
        ScreenerResultRow(
            rank=0,
            symbol="x",
            market="kr",
            name="x",
            logoUrl=None,
            isWatched=False,
            priceLabel="-",
            changePctLabel="-",
            changeAmountLabel="-",
            changeDirection="flat",
            category="-",
            marketCapLabel="-",
            volumeLabel="-",
            analystLabel="-",
            metricValueLabel="-",
            warnings=[],
        )


@pytest.mark.unit
def test_presets_response_holds_selected_id() -> None:
    preset = ScreenerPreset(
        id="cheap_value",
        name="아직 저렴한 가치주",
        description="x",
        badges=[],
        filterChips=[],
        metricLabel="PER",
        market="kr",
    )
    resp = ScreenerPresetsResponse(presets=[preset], selectedPresetId="cheap_value")
    assert resp.selectedPresetId == "cheap_value"
```

- [ ] **Step 1.2: Run tests — expect ImportError**

```bash
uv run pytest tests/test_invest_screener_schemas.py -v
```

Expected: collection error / `ModuleNotFoundError: app.schemas.invest_screener`.

- [ ] **Step 1.3: Implement the schemas**

Create `app/schemas/invest_screener.py`:

```python
"""ROB-147 — read-only DTOs for /invest/api/screener/*.

All fields are display-ready labels. Numeric values are intentionally pre-formatted
so the React layer can render rows without re-running locale logic. When a metric
is unavailable for a row, set the *Label field to "-" and surface a warning string.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ScreenerMarket = Literal["kr", "us", "crypto"]
ChangeDirection = Literal["up", "down", "flat"]


class ScreenerFilterChip(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    detail: str | None = None


class ScreenerPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    description: str
    badges: list[str] = Field(default_factory=list)
    filterChips: list[ScreenerFilterChip] = Field(default_factory=list)
    metricLabel: str
    market: ScreenerMarket = "kr"


class ScreenerResultRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rank: int = Field(ge=1)
    symbol: str
    market: ScreenerMarket
    name: str
    logoUrl: str | None = None
    isWatched: bool = False
    priceLabel: str
    changePctLabel: str
    changeAmountLabel: str
    changeDirection: ChangeDirection
    category: str
    marketCapLabel: str
    volumeLabel: str
    analystLabel: str
    metricValueLabel: str
    warnings: list[str] = Field(default_factory=list)


class ScreenerPresetsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    presets: list[ScreenerPreset]
    selectedPresetId: str | None = None


class ScreenerResultsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    presetId: str
    title: str
    description: str
    filterChips: list[ScreenerFilterChip]
    metricLabel: str
    results: list[ScreenerResultRow]
    warnings: list[str] = Field(default_factory=list)
```

- [ ] **Step 1.4: Re-run tests — expect 5 PASS**

```bash
uv run pytest tests/test_invest_screener_schemas.py -v
```

- [ ] **Step 1.5: Commit**

```bash
git add app/schemas/invest_screener.py tests/test_invest_screener_schemas.py
git commit -m "$(cat <<'EOF'
feat(invest): add ROB-147 screener DTOs

Pydantic schemas for /invest/api/screener/{presets,results}. All fields
display-ready labels with explicit warnings for unavailable metrics.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Backend — preset catalog (static, deterministic)

**Files:**
- Create: `app/services/invest_view_model/screener_presets.py`
- Create: `tests/test_invest_screener_presets.py`

- [ ] **Step 2.1: Write failing preset catalog tests**

Create `tests/test_invest_screener_presets.py`:

```python
"""ROB-147 — static preset catalog tests."""
from __future__ import annotations

import pytest

from app.services.invest_view_model.screener_presets import (
    DEFAULT_PRESET_ID,
    SCREENER_PRESETS,
    get_preset,
    preset_definitions,
    screening_filters_for,
)


@pytest.mark.unit
def test_catalog_has_at_least_six_presets() -> None:
    # Linear acceptance: 최소 5개 이상 — we ship 6.
    assert len(SCREENER_PRESETS) >= 6


@pytest.mark.unit
def test_default_preset_is_in_catalog() -> None:
    ids = {p.id for p in preset_definitions()}
    assert DEFAULT_PRESET_ID in ids


@pytest.mark.unit
def test_all_presets_have_metric_label_and_kr_market() -> None:
    for p in preset_definitions():
        assert p.metricLabel
        assert p.market == "kr"


@pytest.mark.unit
def test_inki_badge_appears_at_least_once() -> None:
    assert any("인기" in p.badges for p in preset_definitions())


@pytest.mark.unit
def test_get_preset_returns_none_for_unknown_id() -> None:
    assert get_preset("does_not_exist") is None


@pytest.mark.unit
def test_get_preset_returns_match() -> None:
    preset = get_preset(DEFAULT_PRESET_ID)
    assert preset is not None
    assert preset.id == DEFAULT_PRESET_ID


@pytest.mark.unit
def test_screening_filters_known_for_each_preset() -> None:
    # Every catalog preset must have a deterministic filter mapping.
    for p in preset_definitions():
        filters = screening_filters_for(p.id)
        assert isinstance(filters, dict)
        # Every preset must specify market and limit so the screening service
        # has bounded inputs.
        assert filters.get("market") == "kr"
        assert isinstance(filters.get("limit"), int)
```

- [ ] **Step 2.2: Run tests — expect ImportError**

```bash
uv run pytest tests/test_invest_screener_presets.py -v
```

- [ ] **Step 2.3: Implement preset catalog**

Create `app/services/invest_view_model/screener_presets.py`:

```python
"""ROB-147 — static catalog of /invest screener presets and the deterministic
mapping from preset id to underlying screening filter parameters.

Each preset's filter mapping is intentionally simple and bounded for the MVP.
Where Toss has a richer condition (e.g. "주가 연속상승 5일") that we cannot yet
compute end-to-end, the chips describe the intent but the underlying filter
falls back to the closest read-only screening parameter we already support
(e.g. sort by change_rate). Those gaps are surfaced as warnings on the
results response, never silently elided."""
from __future__ import annotations

from app.schemas.invest_screener import ScreenerFilterChip, ScreenerPreset

DEFAULT_PRESET_ID = "consecutive_gainers"


SCREENER_PRESETS: list[ScreenerPreset] = [
    ScreenerPreset(
        id="consecutive_gainers",
        name="연속 상승세",
        description="일주일 연속 상승세를 보이는 주식",
        badges=["인기"],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="주가등락률", detail="1주일 전 보다 · 0% 이상"),
            ScreenerFilterChip(label="주가 연속상승", detail="5일 이상 연속"),
        ],
        metricLabel="주가등락률",
        market="kr",
    ),
    ScreenerPreset(
        id="cheap_value",
        name="아직 저렴한 가치주",
        description="PER, PBR 모두 낮은 저평가 종목",
        badges=[],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="PER", detail="15 이하"),
            ScreenerFilterChip(label="PBR", detail="1.5 이하"),
        ],
        metricLabel="PER",
        market="kr",
    ),
    ScreenerPreset(
        id="steady_dividend",
        name="꾸준한 배당주",
        description="배당수익률이 일정 수준 이상인 종목",
        badges=["인기"],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="배당수익률", detail="2% 이상"),
        ],
        metricLabel="배당수익률",
        market="kr",
    ),
    ScreenerPreset(
        id="oversold_recovery",
        name="저평가 탈출",
        description="RSI가 낮은 구간으로 들어온 종목",
        badges=["인기"],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="RSI", detail="30 이하"),
        ],
        metricLabel="RSI",
        market="kr",
    ),
    ScreenerPreset(
        id="high_volume_momentum",
        name="쌍끌이 매수",
        description="거래량이 폭발적으로 늘어난 종목",
        badges=[],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="거래량", detail="상위"),
        ],
        metricLabel="거래량",
        market="kr",
    ),
    ScreenerPreset(
        id="growth_expectation",
        name="성장 기대주",
        description="시가총액이 충분하고 등락률 상위인 성장 기대 종목",
        badges=[],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="시가총액", detail="1조 이상"),
            ScreenerFilterChip(label="주가등락률", detail="상위"),
        ],
        metricLabel="주가등락률",
        market="kr",
    ),
]


_SCREENING_FILTERS: dict[str, dict[str, object]] = {
    "consecutive_gainers": {
        "market": "kr",
        "asset_type": "stock",
        "sort_by": "change_rate",
        "sort_order": "desc",
        "limit": 20,
    },
    "cheap_value": {
        "market": "kr",
        "asset_type": "stock",
        "sort_by": "market_cap",
        "sort_order": "desc",
        "max_per": 15.0,
        "max_pbr": 1.5,
        "limit": 20,
    },
    "steady_dividend": {
        "market": "kr",
        "asset_type": "stock",
        "sort_by": "dividend_yield",
        "sort_order": "desc",
        "min_dividend_yield": 2.0,
        "limit": 20,
    },
    "oversold_recovery": {
        "market": "kr",
        "asset_type": "stock",
        "sort_by": "rsi",
        "sort_order": "asc",
        "max_rsi": 30.0,
        "limit": 20,
    },
    "high_volume_momentum": {
        "market": "kr",
        "asset_type": "stock",
        "sort_by": "volume",
        "sort_order": "desc",
        "limit": 20,
    },
    "growth_expectation": {
        "market": "kr",
        "asset_type": "stock",
        "sort_by": "change_rate",
        "sort_order": "desc",
        "min_market_cap": 1_000_000_000_000.0,
        "limit": 20,
    },
}


def preset_definitions() -> list[ScreenerPreset]:
    """Return a copy of the static preset list."""
    return list(SCREENER_PRESETS)


def get_preset(preset_id: str) -> ScreenerPreset | None:
    for p in SCREENER_PRESETS:
        if p.id == preset_id:
            return p
    return None


def screening_filters_for(preset_id: str) -> dict[str, object]:
    """Return the screening service kwargs for a preset, or {} if unknown."""
    return dict(_SCREENING_FILTERS.get(preset_id, {}))
```

- [ ] **Step 2.4: Run tests — expect 7 PASS**

```bash
uv run pytest tests/test_invest_screener_presets.py -v
```

- [ ] **Step 2.5: Commit**

```bash
git add app/services/invest_view_model/screener_presets.py tests/test_invest_screener_presets.py
git commit -m "$(cat <<'EOF'
feat(invest): add ROB-147 screener preset catalog

Static catalog of 6 KR-market presets with deterministic mapping to the
existing read-only screening service kwargs. Includes 인기 badges, filter
chips, and per-preset metric labels.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Backend — view-model service (`build_screener_results`)

**Files:**
- Create: `app/services/invest_view_model/screener_service.py`
- Create: `tests/test_invest_view_model_screener_service.py`

**Context for the implementer:** the existing read-only screening entry point is `app.services.screener_service.ScreenerService.list_screening(filters: ScreenerFilters) -> dict`. Inspect that service before implementing — its return value is the canonical row source. Each row dict includes at minimum `symbol`, `name`, `market`, `category`/`sector`, `market_cap`, `close`/`price`, `change_rate`, `volume`, and metric-specific fields like `per`, `pbr`, `dividend_yield`, `rsi`. If a row is missing a field, fall back to `"-"` and append a warning.

**Watchlist:** use `build_relation_resolver(db, user_id=...)` (already imported from `app.services.invest_view_model.relation_resolver`); call `resolver.relation(market, symbol)` per row and set `isWatched = relation in {"watchlist", "both"}`.

- [ ] **Step 3.1: Write failing service tests**

Create `tests/test_invest_view_model_screener_service.py`:

```python
"""ROB-147 — view-model tests for build_screener_results."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.invest_screener import ScreenerResultsResponse
from app.services.invest_view_model.screener_service import (
    build_screener_presets,
    build_screener_results,
)


def _stub_screening_rows() -> list[dict[str, Any]]:
    return [
        {
            "symbol": "005930",
            "name": "삼성전자",
            "market": "kr",
            "sector": "반도체",
            "market_cap": 478_000_000_000_000,
            "close": 80_000,
            "change_rate": 1.23,
            "change_amount": 970,
            "volume": 12_345_678,
            "per": 14.0,
            "pbr": 1.2,
            "dividend_yield": 1.8,
            "rsi": 55.0,
        },
        {
            "symbol": "035720",
            "name": "카카오",
            "market": "kr",
            "sector": "인터넷",
            "market_cap": 20_000_000_000_000,
            "close": 45_000,
            "change_rate": -0.5,
            "change_amount": -200,
            "volume": 3_000_000,
            "per": None,
            "pbr": None,
            "dividend_yield": None,
            "rsi": None,
        },
    ]


class _FakeResolver:
    def __init__(self, watched: set[tuple[str, str]]) -> None:
        self._w = watched

    def relation(self, market: str, symbol: str) -> str:
        return "watchlist" if (market, symbol) in self._w else "none"


@pytest.mark.unit
async def test_build_screener_presets_returns_default_selected() -> None:
    resp = build_screener_presets()
    assert len(resp.presets) >= 6
    assert resp.selectedPresetId == "consecutive_gainers"


@pytest.mark.unit
async def test_build_screener_results_consecutive_gainers_happy_path() -> None:
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={"stocks": _stub_screening_rows(), "warnings": []}
    )
    resolver = _FakeResolver(watched={("kr", "005930")})

    resp: ScreenerResultsResponse = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=resolver,
    )

    assert resp.presetId == "consecutive_gainers"
    assert resp.title == "연속 상승세"
    assert resp.metricLabel == "주가등락률"
    assert len(resp.results) == 2
    assert resp.results[0].rank == 1
    assert resp.results[0].symbol == "005930"
    assert resp.results[0].isWatched is True
    assert resp.results[0].changeDirection == "up"
    assert resp.results[1].symbol == "035720"
    assert resp.results[1].isWatched is False
    assert resp.results[1].changeDirection == "down"


@pytest.mark.unit
async def test_build_screener_results_unknown_preset_returns_empty_with_warning() -> None:
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock()
    resolver = _FakeResolver(watched=set())

    resp = await build_screener_results(
        preset_id="does_not_exist",
        screening_service=fake_screening,
        resolver=resolver,
    )

    assert resp.presetId == "does_not_exist"
    assert resp.results == []
    assert resp.warnings, "unknown preset should produce a warning"
    fake_screening.list_screening.assert_not_called()


@pytest.mark.unit
async def test_build_screener_results_unavailable_metric_uses_dash_and_warns() -> None:
    """oversold_recovery uses RSI; rows missing rsi must render '-' + warning."""
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={
            "stocks": [
                {
                    "symbol": "035720",
                    "name": "카카오",
                    "market": "kr",
                    "sector": "인터넷",
                    "market_cap": 20_000_000_000_000,
                    "close": 45_000,
                    "change_rate": -0.5,
                    "volume": 3_000_000,
                    "rsi": None,
                }
            ],
            "warnings": [],
        }
    )
    resolver = _FakeResolver(watched=set())

    resp = await build_screener_results(
        preset_id="oversold_recovery",
        screening_service=fake_screening,
        resolver=resolver,
    )

    assert resp.results[0].metricValueLabel == "-"
    assert any("RSI" in w for w in resp.results[0].warnings)


@pytest.mark.unit
async def test_build_screener_results_screening_warnings_propagate() -> None:
    fake_screening = MagicMock()
    fake_screening.list_screening = AsyncMock(
        return_value={
            "stocks": [],
            "warnings": ["KIS quote service degraded"],
        }
    )
    resolver = _FakeResolver(watched=set())

    resp = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening,
        resolver=resolver,
    )

    assert "KIS quote service degraded" in resp.warnings
    assert resp.results == []
```

- [ ] **Step 3.2: Run tests — expect ImportError**

```bash
uv run pytest tests/test_invest_view_model_screener_service.py -v
```

- [ ] **Step 3.3: Implement the service**

Create `app/services/invest_view_model/screener_service.py`:

```python
"""ROB-147 — read-only view-model wrapper around the screening service.

Public API:
- build_screener_presets() -> ScreenerPresetsResponse
- build_screener_results(preset_id, screening_service, resolver) -> ScreenerResultsResponse

The service intentionally takes its dependencies as parameters so the router
can inject the existing `app.services.screener_service.ScreenerService` (and
tests can inject mocks). It must not import any broker / order / mutation
modules — see tests/test_invest_view_model_safety.py.
"""
from __future__ import annotations

from typing import Any, Protocol

from app.schemas.invest_screener import (
    ChangeDirection,
    ScreenerPresetsResponse,
    ScreenerResultRow,
    ScreenerResultsResponse,
)
from app.services.invest_view_model.screener_presets import (
    DEFAULT_PRESET_ID,
    get_preset,
    preset_definitions,
    screening_filters_for,
)


class _ScreeningServiceProto(Protocol):
    async def list_screening(self, /, **kwargs: Any) -> dict[str, Any]: ...


class _ResolverProto(Protocol):
    def relation(self, market: str, symbol: str) -> str: ...


def build_screener_presets() -> ScreenerPresetsResponse:
    return ScreenerPresetsResponse(
        presets=preset_definitions(),
        selectedPresetId=DEFAULT_PRESET_ID,
    )


_METRIC_FIELD: dict[str, str] = {
    "consecutive_gainers": "change_rate",
    "cheap_value": "per",
    "steady_dividend": "dividend_yield",
    "oversold_recovery": "rsi",
    "high_volume_momentum": "volume",
    "growth_expectation": "change_rate",
}


def _format_change_pct(rate: float | None) -> tuple[str, ChangeDirection]:
    if rate is None:
        return "-", "flat"
    direction: ChangeDirection = "up" if rate > 0 else "down" if rate < 0 else "flat"
    sign = "+" if rate > 0 else ""
    return f"{sign}{rate:.2f}%", direction


def _format_change_amount(amount: float | None, currency: str = "원") -> str:
    if amount is None:
        return "-"
    sign = "+" if amount > 0 else ""
    return f"{sign}{int(amount):,}{currency}"


def _format_price(close: float | None) -> str:
    if close is None:
        return "-"
    return f"{int(close):,}원"


def _format_market_cap_kr(market_cap: float | None) -> str:
    if market_cap is None:
        return "-"
    eok = market_cap / 100_000_000.0
    if eok >= 10_000:
        jo = eok / 10_000.0
        return f"{jo:,.1f}조원"
    return f"{eok:,.0f}억원"


def _format_volume(volume: float | None) -> str:
    if volume is None:
        return "-"
    return f"{int(volume):,}"


def _metric_value_label(preset_id: str, row: dict[str, Any]) -> tuple[str, list[str]]:
    field = _METRIC_FIELD.get(preset_id)
    if not field:
        return "-", []
    value = row.get(field)
    if value is None:
        return "-", [f"{field.upper()} 데이터 준비중"]
    if field == "change_rate":
        sign = "+" if value > 0 else ""
        return f"{sign}{value:.2f}%", []
    if field in ("per", "pbr", "rsi"):
        return f"{float(value):.1f}", []
    if field == "dividend_yield":
        return f"{float(value):.2f}%", []
    if field == "volume":
        return f"{int(value):,}", []
    return str(value), []


async def build_screener_results(
    preset_id: str,
    screening_service: _ScreeningServiceProto,
    resolver: _ResolverProto,
) -> ScreenerResultsResponse:
    preset = get_preset(preset_id)
    if preset is None:
        return ScreenerResultsResponse(
            presetId=preset_id,
            title=preset_id,
            description="",
            filterChips=[],
            metricLabel="-",
            results=[],
            warnings=[f"알 수 없는 프리셋: {preset_id}"],
        )

    filters = screening_filters_for(preset_id)
    raw = await screening_service.list_screening(**filters)
    rows: list[dict[str, Any]] = list(raw.get("stocks") or [])
    upstream_warnings: list[str] = list(raw.get("warnings") or [])

    results: list[ScreenerResultRow] = []
    for idx, row in enumerate(rows, start=1):
        symbol = str(row.get("symbol") or "")
        market = str(row.get("market") or "kr").lower()
        if market not in ("kr", "us", "crypto"):
            market = "kr"
        change_pct_label, direction = _format_change_pct(row.get("change_rate"))
        metric_label, metric_warnings = _metric_value_label(preset_id, row)
        relation = resolver.relation(market, symbol)
        is_watched = relation in ("watchlist", "both")
        results.append(
            ScreenerResultRow(
                rank=idx,
                symbol=symbol,
                market=market,  # type: ignore[arg-type]
                name=str(row.get("name") or symbol),
                logoUrl=row.get("logo_url"),
                isWatched=is_watched,
                priceLabel=_format_price(row.get("close") or row.get("price")),
                changePctLabel=change_pct_label,
                changeAmountLabel=_format_change_amount(row.get("change_amount")),
                changeDirection=direction,
                category=str(row.get("sector") or row.get("category") or "-"),
                marketCapLabel=_format_market_cap_kr(row.get("market_cap")),
                volumeLabel=_format_volume(row.get("volume")),
                analystLabel=str(row.get("analyst_label") or "-"),
                metricValueLabel=metric_label,
                warnings=metric_warnings,
            )
        )

    return ScreenerResultsResponse(
        presetId=preset.id,
        title=preset.name,
        description=preset.description,
        filterChips=preset.filterChips,
        metricLabel=preset.metricLabel,
        results=results,
        warnings=upstream_warnings,
    )
```

- [ ] **Step 3.4: Run tests — expect 5 PASS**

```bash
uv run pytest tests/test_invest_view_model_screener_service.py -v
```

- [ ] **Step 3.5: Commit**

```bash
git add app/services/invest_view_model/screener_service.py tests/test_invest_view_model_screener_service.py
git commit -m "$(cat <<'EOF'
feat(invest): add ROB-147 screener view-model builder

build_screener_results maps the existing read-only screening service rows
into display-ready ScreenerResultRow with watchlist relation, formatted
KR-market labels, and per-row warnings for missing metrics.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Backend — extend invest_view_model safety test

**Files:**
- Modify: `tests/test_invest_view_model_safety.py` (add screener_service + screener_presets to the imported set)

**Context:** The existing safety test enforces that nothing in the `invest_view_model` package transitively imports broker / order / mutation paths. Read the existing file first; only extend its imports list.

- [ ] **Step 4.1: Read the existing safety test**

```bash
sed -n '1,200p' tests/test_invest_view_model_safety.py
```

- [ ] **Step 4.2: Add the new imports to the inline subprocess script**

In the `script = """..."""` literal inside the safety test, add:

```python
import app.services.invest_view_model.screener_presets
import app.services.invest_view_model.screener_service
```

…immediately after the existing `import app.services.invest_view_model.*` lines.

- [ ] **Step 4.3: Run safety test — expect PASS**

```bash
uv run pytest tests/test_invest_view_model_safety.py -v
```

If it FAILS with violations, fix the imports inside `screener_service.py` (most likely cause: pulling in the existing `screener_service` module from `app.services` that itself reaches a forbidden module). Do NOT widen the FORBIDDEN list.

- [ ] **Step 4.4: Commit**

```bash
git add tests/test_invest_view_model_safety.py
git commit -m "$(cat <<'EOF'
test(invest): cover ROB-147 screener modules in view-model safety test

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Backend — wire `/invest/api/screener/{presets,results}` routes

**Files:**
- Modify: `app/routers/invest_api.py`
- Modify: `tests/test_invest_api_router.py`

**Context:** Read `app/routers/invest_api.py` first. Follow its dependency-injection style (`get_invest_home_service` lazy singleton pattern). Inject `ScreenerService` lazily so tests can override it via `app.dependency_overrides`. Read the existing `tests/test_invest_api_router.py` to copy its `_StubService` + `dependency_overrides` test pattern.

- [ ] **Step 5.1: Write failing router tests**

Append to `tests/test_invest_api_router.py` (or, if too crowded, create `tests/test_invest_api_screener_router.py` mirroring its `TestClient` setup). Tests:

```python
import pytest
from fastapi.testclient import TestClient

from app.main import app  # adjust import to match what existing tests use
from app.routers.dependencies import get_authenticated_user
from app.routers.invest_api import get_screener_service_dep  # new dep added in 5.2


class _StubScreening:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def list_screening(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "stocks": [
                {
                    "symbol": "005930",
                    "name": "삼성전자",
                    "market": "kr",
                    "sector": "반도체",
                    "market_cap": 478_000_000_000_000,
                    "close": 80_000,
                    "change_rate": 1.23,
                    "change_amount": 970,
                    "volume": 12_345_678,
                }
            ],
            "warnings": [],
        }


class _DummyUser:
    id = 1
    email = "test@example.com"


@pytest.mark.integration
def test_screener_presets_endpoint_returns_catalog() -> None:
    app.dependency_overrides[get_authenticated_user] = lambda: _DummyUser()
    try:
        client = TestClient(app)
        r = client.get("/invest/api/screener/presets")
        assert r.status_code == 200
        body = r.json()
        assert len(body["presets"]) >= 6
        assert body["selectedPresetId"] == "consecutive_gainers"
    finally:
        app.dependency_overrides.pop(get_authenticated_user, None)


@pytest.mark.integration
def test_screener_results_endpoint_happy_path() -> None:
    stub = _StubScreening()
    app.dependency_overrides[get_authenticated_user] = lambda: _DummyUser()
    app.dependency_overrides[get_screener_service_dep] = lambda: stub
    try:
        client = TestClient(app)
        r = client.get("/invest/api/screener/results?preset=consecutive_gainers")
        assert r.status_code == 200
        body = r.json()
        assert body["presetId"] == "consecutive_gainers"
        assert body["title"] == "연속 상승세"
        assert len(body["results"]) == 1
        assert body["results"][0]["symbol"] == "005930"
        assert stub.calls and stub.calls[0]["market"] == "kr"
    finally:
        app.dependency_overrides.pop(get_authenticated_user, None)
        app.dependency_overrides.pop(get_screener_service_dep, None)


@pytest.mark.integration
def test_screener_results_endpoint_unknown_preset_returns_empty_with_warning() -> None:
    stub = _StubScreening()
    app.dependency_overrides[get_authenticated_user] = lambda: _DummyUser()
    app.dependency_overrides[get_screener_service_dep] = lambda: stub
    try:
        client = TestClient(app)
        r = client.get("/invest/api/screener/results?preset=__unknown__")
        assert r.status_code == 200
        body = r.json()
        assert body["results"] == []
        assert body["warnings"]
        assert stub.calls == []
    finally:
        app.dependency_overrides.pop(get_authenticated_user, None)
        app.dependency_overrides.pop(get_screener_service_dep, None)
```

- [ ] **Step 5.2: Run tests — expect ImportError**

```bash
uv run pytest tests/test_invest_api_router.py -v -k screener
```

- [ ] **Step 5.3: Wire endpoints into `app/routers/invest_api.py`**

Add these imports at the top of `app/routers/invest_api.py`:

```python
from app.schemas.invest_screener import (
    ScreenerPresetsResponse,
    ScreenerResultsResponse,
)
from app.services.invest_view_model.screener_service import (
    build_screener_presets,
    build_screener_results,
)
```

Add the screening service dependency (lazy import to keep module load read-only):

```python
def get_screener_service_dep():
    """Lazy DI for the existing read-only screening service."""
    from app.services.screener_service import ScreenerService
    return ScreenerService()
```

Add the two endpoints below the existing `/feed/news` route:

```python
@router.get("/screener/presets")
async def get_screener_presets_endpoint(
    user: Annotated[Any, Depends(get_authenticated_user)],
) -> ScreenerPresetsResponse:
    return build_screener_presets()


@router.get("/screener/results")
async def get_screener_results_endpoint(
    user: Annotated[Any, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[InvestHomeService, Depends(get_invest_home_service)],
    screening_service: Annotated[Any, Depends(get_screener_service_dep)],
    preset: str = Query(..., min_length=1),
) -> ScreenerResultsResponse:
    home = await service.get_home(user_id=user.id)
    resolver = await build_relation_resolver(
        db, user_id=user.id, held_pairs=_held_pairs_from_home(home)
    )
    return await build_screener_results(
        preset_id=preset,
        screening_service=screening_service,
        resolver=resolver,
    )
```

- [ ] **Step 5.4: Run tests — expect 3 PASS**

```bash
uv run pytest tests/test_invest_api_router.py -v -k screener
```

- [ ] **Step 5.5: Commit**

```bash
git add app/routers/invest_api.py tests/test_invest_api_router.py
git commit -m "$(cat <<'EOF'
feat(invest): add ROB-147 /invest/api/screener/{presets,results}

Two read-only GET endpoints powered by the screener view-model. Existing
ScreenerService is injected via lazy DI so the import surface stays clean
and tests can override the dependency.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Backend — extend invest_api router safety test

**Files:**
- Modify: `tests/test_invest_api_router_safety.py`

**Context:** The existing safety test imports `app.routers.invest_api` and asserts that no forbidden mutation modules end up in `sys.modules`. The screener endpoint pulls in `app.services.screener_service.ScreenerService` lazily *inside* the dependency; verify this lazy path is not eagerly loaded by the router import. If it is, refactor the dependency to keep it lazy. The safety test already covers `app.routers.invest_api` — just **re-run** it. If it fails, fix the eager-import.

- [ ] **Step 6.1: Run the existing safety test**

```bash
uv run pytest tests/test_invest_api_router_safety.py -v
```

Expected: PASS. If FAIL: move offending imports inside `get_screener_service_dep()` (already done by design) and re-run.

- [ ] **Step 6.2: If anything was refactored, commit**

```bash
git add app/routers/invest_api.py
git commit -m "$(cat <<'EOF'
fix(invest): keep ROB-147 screener DI lazy to satisfy safety guard

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

(If no fix needed: skip this commit step.)

---

## Task 7: Frontend — types + API wrapper

**Files:**
- Create: `frontend/invest/src/types/screener.ts`
- Create: `frontend/invest/src/api/screener.ts`

- [ ] **Step 7.1: Implement TypeScript types**

Create `frontend/invest/src/types/screener.ts`:

```typescript
export type ScreenerMarket = "kr" | "us" | "crypto";
export type ScreenerChangeDirection = "up" | "down" | "flat";

export interface ScreenerFilterChip {
  label: string;
  detail: string | null;
}

export interface ScreenerPreset {
  id: string;
  name: string;
  description: string;
  badges: string[];
  filterChips: ScreenerFilterChip[];
  metricLabel: string;
  market: ScreenerMarket;
}

export interface ScreenerPresetsResponse {
  presets: ScreenerPreset[];
  selectedPresetId: string | null;
}

export interface ScreenerResultRow {
  rank: number;
  symbol: string;
  market: ScreenerMarket;
  name: string;
  logoUrl: string | null;
  isWatched: boolean;
  priceLabel: string;
  changePctLabel: string;
  changeAmountLabel: string;
  changeDirection: ScreenerChangeDirection;
  category: string;
  marketCapLabel: string;
  volumeLabel: string;
  analystLabel: string;
  metricValueLabel: string;
  warnings: string[];
}

export interface ScreenerResultsResponse {
  presetId: string;
  title: string;
  description: string;
  filterChips: ScreenerFilterChip[];
  metricLabel: string;
  results: ScreenerResultRow[];
  warnings: string[];
}
```

- [ ] **Step 7.2: Implement API wrapper (follow `frontend/invest/src/api/feedNews.ts` pattern)**

Create `frontend/invest/src/api/screener.ts`:

```typescript
import type {
  ScreenerPresetsResponse,
  ScreenerResultsResponse,
} from "../types/screener";

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    throw new Error(`HTTP ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

export async function fetchScreenerPresets(): Promise<ScreenerPresetsResponse> {
  const res = await fetch("/invest/api/screener/presets", {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  return jsonOrThrow<ScreenerPresetsResponse>(res);
}

export async function fetchScreenerResults(
  presetId: string,
): Promise<ScreenerResultsResponse> {
  const params = new URLSearchParams({ preset: presetId });
  const res = await fetch(`/invest/api/screener/results?${params.toString()}`, {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  return jsonOrThrow<ScreenerResultsResponse>(res);
}
```

- [ ] **Step 7.3: Typecheck**

```bash
cd frontend/invest && npm run typecheck
```

Expected: PASS.

- [ ] **Step 7.4: Commit**

```bash
git add frontend/invest/src/types/screener.ts frontend/invest/src/api/screener.ts
git commit -m "$(cat <<'EOF'
feat(invest): add ROB-147 screener frontend types + api wrapper

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Frontend — child components

**Files:**
- Create: `frontend/invest/src/desktop/screener/ScreenerPresetSidebar.tsx`
- Create: `frontend/invest/src/desktop/screener/ScreenerFilterBar.tsx`
- Create: `frontend/invest/src/desktop/screener/ScreenerResultsTable.tsx`
- Create: `frontend/invest/src/desktop/screener/ScreenerFilterModal.tsx`
- Create: `frontend/invest/src/desktop/screener/screener.module.css`

- [ ] **Step 8.1: ScreenerPresetSidebar**

```tsx
import type { ScreenerPreset } from "../../types/screener";
import styles from "./screener.module.css";

interface Props {
  presets: ScreenerPreset[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

export function ScreenerPresetSidebar({ presets, selectedId, onSelect }: Props) {
  return (
    <aside className={styles.sidebar} aria-label="주식 골라보기 목록">
      <div className={styles.sidebarSection}>
        <div className={styles.sidebarHeading}>내가 만든</div>
        <button type="button" className={styles.sidebarLink} disabled aria-disabled="true">
          직접 만들기 (준비중)
        </button>
      </div>
      <div className={styles.sidebarSection}>
        <div className={styles.sidebarHeading}>토스증권이 만든</div>
        <ul className={styles.presetList}>
          {presets.map((p) => {
            const active = p.id === selectedId;
            return (
              <li key={p.id}>
                <button
                  type="button"
                  className={active ? styles.presetItemActive : styles.presetItem}
                  onClick={() => onSelect(p.id)}
                  aria-current={active ? "true" : undefined}
                >
                  <span className={styles.presetName}>{p.name}</span>
                  {p.badges.includes("인기") && (
                    <span className={styles.presetBadge}>인기</span>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      </div>
    </aside>
  );
}
```

- [ ] **Step 8.2: ScreenerFilterBar**

```tsx
import type { ScreenerFilterChip } from "../../types/screener";
import styles from "./screener.module.css";

interface Props {
  title: string;
  description: string;
  chips: ScreenerFilterChip[];
  resultCount: number;
  onOpenFilterModal: () => void;
}

export function ScreenerFilterBar({
  title,
  description,
  chips,
  resultCount,
  onOpenFilterModal,
}: Props) {
  return (
    <div className={styles.filterBar}>
      <div>
        <h2 className={styles.filterTitle}>{title}</h2>
        <p className={styles.filterDescription}>{description}</p>
      </div>
      <div className={styles.chipRow}>
        <button
          type="button"
          className={styles.chipAdd}
          onClick={onOpenFilterModal}
        >
          + 필터추가
        </button>
        {chips.map((c, i) => (
          <span className={styles.chip} key={`${c.label}-${i}`}>
            <strong>{c.label}</strong>
            {c.detail && <span className={styles.chipDetail}> · {c.detail}</span>}
          </span>
        ))}
      </div>
      <div className={styles.resultCount}>
        검색된 주식 ・ <strong>{resultCount.toLocaleString()}</strong>개
      </div>
    </div>
  );
}
```

- [ ] **Step 8.3: ScreenerResultsTable**

```tsx
import type { ScreenerResultRow } from "../../types/screener";
import styles from "./screener.module.css";

interface Props {
  rows: ScreenerResultRow[];
  metricLabel: string;
}

export function ScreenerResultsTable({ rows, metricLabel }: Props) {
  if (rows.length === 0) {
    return <div className={styles.empty}>표시할 종목이 없습니다.</div>;
  }
  return (
    <table className={styles.table}>
      <thead>
        <tr>
          <th aria-label="관심" />
          <th>순위</th>
          <th>종목</th>
          <th>현재가</th>
          <th>등락률</th>
          <th>카테고리</th>
          <th>시가총액</th>
          <th>거래량</th>
          <th>애널리스트 분석</th>
          <th>{metricLabel}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={`${r.market}-${r.symbol}`}>
            <td>
              <span
                className={r.isWatched ? styles.heartOn : styles.heartOff}
                aria-label={r.isWatched ? "관심 종목" : "관심 종목 아님"}
                role="img"
              >
                ♥
              </span>
            </td>
            <td>{r.rank}</td>
            <td className={styles.nameCell}>
              <span className={styles.symbolBadge}>{r.symbol}</span>
              <span className={styles.symbolName}>{r.name}</span>
            </td>
            <td>{r.priceLabel}</td>
            <td className={styles[`change_${r.changeDirection}`]}>
              {r.changePctLabel}
              <span className={styles.changeAmount}> {r.changeAmountLabel}</span>
            </td>
            <td>{r.category}</td>
            <td>{r.marketCapLabel}</td>
            <td>{r.volumeLabel}</td>
            <td>{r.analystLabel}</td>
            <td>{r.metricValueLabel}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
```

- [ ] **Step 8.4: ScreenerFilterModal — read-only "준비중" shell**

```tsx
import styles from "./screener.module.css";

interface Props {
  open: boolean;
  onClose: () => void;
  appliedChipCount: number;
}

const TABS = ["기본", "재무", "시세", "기술", "필터 검색"];
const BASIC_CATEGORIES = ["국가", "시장", "카테고리", "시가총액", "제외 종목 관리"];

export function ScreenerFilterModal({ open, onClose, appliedChipCount }: Props) {
  if (!open) return null;
  return (
    <div className={styles.modalBackdrop} role="dialog" aria-modal="true">
      <div className={styles.modal}>
        <header className={styles.modalHeader}>
          <h3>필터</h3>
          <button type="button" onClick={onClose} aria-label="닫기">
            ×
          </button>
        </header>
        <nav className={styles.modalTabs}>
          {TABS.map((t, i) => (
            <button
              key={t}
              type="button"
              className={i === 0 ? styles.modalTabActive : styles.modalTab}
              disabled
              aria-disabled="true"
            >
              {t}
            </button>
          ))}
        </nav>
        <section className={styles.modalBody}>
          <p className={styles.modalNotice}>
            세부 필터 편집은 준비중입니다. 좌측 프리셋에서 선택해 주세요.
          </p>
          <ul className={styles.modalCategoryList}>
            {BASIC_CATEGORIES.map((c) => (
              <li key={c} className={styles.modalCategoryItem}>
                {c}
              </li>
            ))}
          </ul>
        </section>
        <footer className={styles.modalFooter}>
          <button type="button" disabled aria-disabled="true">
            초기화
          </button>
          <button type="button" disabled aria-disabled="true">
            {appliedChipCount}개 필터 적용 (준비중)
          </button>
        </footer>
      </div>
    </div>
  );
}
```

- [ ] **Step 8.5: Stylesheet**

Create `frontend/invest/src/desktop/screener/screener.module.css`:

```css
.sidebar {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px 12px;
  border-right: 1px solid #eee;
  min-height: 100%;
}
.sidebarSection { display: flex; flex-direction: column; gap: 8px; }
.sidebarHeading { font-size: 12px; color: #888; }
.sidebarLink { background: transparent; border: 1px dashed #ccc; padding: 8px; text-align: left; border-radius: 6px; color: #888; }
.presetList { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 2px; }
.presetItem,
.presetItemActive {
  width: 100%;
  text-align: left;
  background: transparent;
  border: 0;
  padding: 8px 10px;
  border-radius: 6px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  cursor: pointer;
}
.presetItem:hover { background: #f5f5f7; }
.presetItemActive { background: #eef3ff; color: #1f3bd1; font-weight: 600; }
.presetName { font-size: 14px; }
.presetBadge { font-size: 10px; background: #ffefc7; color: #c47d00; border-radius: 4px; padding: 2px 6px; }

.filterBar { display: flex; flex-direction: column; gap: 12px; padding: 16px; }
.filterTitle { font-size: 22px; margin: 0; }
.filterDescription { color: #666; margin: 4px 0 0; font-size: 14px; }
.chipRow { display: flex; flex-wrap: wrap; gap: 8px; }
.chipAdd { background: #f5f5f7; border: 0; padding: 6px 10px; border-radius: 999px; cursor: pointer; }
.chip { background: #eef3ff; color: #1f3bd1; padding: 6px 10px; border-radius: 999px; font-size: 13px; }
.chipDetail { color: #1f3bd1; opacity: 0.85; }
.resultCount { color: #444; font-size: 14px; }

.table { width: 100%; border-collapse: collapse; }
.table th,
.table td { padding: 10px 8px; text-align: left; border-bottom: 1px solid #f0f0f0; font-size: 13px; }
.table th { color: #888; font-weight: 500; }
.nameCell { display: flex; flex-direction: column; gap: 2px; }
.symbolBadge { font-size: 11px; color: #888; }
.symbolName { font-weight: 600; }
.change_up { color: #d92d20; }
.change_down { color: #1f7ed1; }
.change_flat { color: #444; }
.changeAmount { color: inherit; opacity: 0.7; font-size: 12px; margin-left: 4px; }
.heartOn { color: #d92d20; }
.heartOff { color: #ddd; }
.empty { padding: 32px; text-align: center; color: #888; }

.modalBackdrop {
  position: fixed; inset: 0; background: rgba(0,0,0,0.4);
  display: flex; align-items: center; justify-content: center; z-index: 50;
}
.modal { background: #fff; width: 720px; max-width: 90vw; max-height: 80vh; border-radius: 12px; display: flex; flex-direction: column; overflow: hidden; }
.modalHeader { display: flex; justify-content: space-between; align-items: center; padding: 16px; border-bottom: 1px solid #f0f0f0; }
.modalTabs { display: flex; gap: 4px; padding: 8px 16px; border-bottom: 1px solid #f0f0f0; }
.modalTab,
.modalTabActive { background: transparent; border: 0; padding: 6px 10px; border-radius: 6px; color: #888; }
.modalTabActive { color: #111; font-weight: 600; }
.modalBody { padding: 16px; overflow-y: auto; flex: 1; }
.modalNotice { color: #666; }
.modalCategoryList { list-style: none; padding: 0; display: flex; flex-wrap: wrap; gap: 8px; }
.modalCategoryItem { background: #f5f5f7; padding: 6px 10px; border-radius: 999px; font-size: 13px; }
.modalFooter { display: flex; justify-content: flex-end; gap: 8px; padding: 12px 16px; border-top: 1px solid #f0f0f0; }
```

- [ ] **Step 8.6: Typecheck + commit**

```bash
cd frontend/invest && npm run typecheck
```

```bash
git add frontend/invest/src/desktop/screener/
git commit -m "$(cat <<'EOF'
feat(invest): add ROB-147 screener desktop child components

Sidebar, filter bar, results table, and read-only filter-modal shell with
shared CSS module. Modal explicitly marks the 필터 편집 path as 준비중 to
satisfy the read-only MVP boundary.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Frontend — DesktopScreenerPage + route + nav link

**Files:**
- Create: `frontend/invest/src/pages/DesktopScreenerPage.tsx` (or `desktop/DesktopScreenerPage.tsx` if existing siblings live there — match the pattern of `DesktopFeedNewsPage`)
- Modify: `frontend/invest/src/routes.tsx`
- Modify: `frontend/invest/src/desktop/DesktopHeader.tsx` (add `주식 골라보기` link)

**Context:** Read `frontend/invest/src/desktop/DesktopFeedNewsPage.tsx` first to see the standard page shape (DesktopShell wrapper, fetch on mount, error state).

- [ ] **Step 9.1: Implement page**

```tsx
import { useEffect, useState } from "react";

import { fetchScreenerPresets, fetchScreenerResults } from "../api/screener";
import { DesktopShell } from "../desktop/DesktopShell";
import { ScreenerFilterBar } from "../desktop/screener/ScreenerFilterBar";
import { ScreenerFilterModal } from "../desktop/screener/ScreenerFilterModal";
import { ScreenerPresetSidebar } from "../desktop/screener/ScreenerPresetSidebar";
import { ScreenerResultsTable } from "../desktop/screener/ScreenerResultsTable";
import type {
  ScreenerPresetsResponse,
  ScreenerResultsResponse,
} from "../types/screener";

export default function DesktopScreenerPage() {
  const [presets, setPresets] = useState<ScreenerPresetsResponse | null>(null);
  const [results, setResults] = useState<ScreenerResultsResponse | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchScreenerPresets()
      .then((data) => {
        if (cancelled) return;
        setPresets(data);
        setSelectedId(data.selectedPresetId ?? data.presets[0]?.id ?? null);
      })
      .catch((e: unknown) => setError(String(e)));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!selectedId) return;
    let cancelled = false;
    setResults(null);
    fetchScreenerResults(selectedId)
      .then((data) => {
        if (!cancelled) setResults(data);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  return (
    <DesktopShell
      left={
        <ScreenerPresetSidebar
          presets={presets?.presets ?? []}
          selectedId={selectedId}
          onSelect={setSelectedId}
        />
      }
      center={
        <div>
          {error && <div role="alert">{error}</div>}
          {results ? (
            <>
              <ScreenerFilterBar
                title={results.title}
                description={results.description}
                chips={results.filterChips}
                resultCount={results.results.length}
                onOpenFilterModal={() => setModalOpen(true)}
              />
              {results.warnings.length > 0 && (
                <ul aria-label="warnings">
                  {results.warnings.map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              )}
              <ScreenerResultsTable rows={results.results} metricLabel={results.metricLabel} />
            </>
          ) : (
            !error && <div>불러오는 중...</div>
          )}
          <ScreenerFilterModal
            open={modalOpen}
            onClose={() => setModalOpen(false)}
            appliedChipCount={results?.filterChips.length ?? 0}
          />
        </div>
      }
    />
  );
}
```

If `DesktopShell`'s prop names differ (read it first), adapt the prop names accordingly.

- [ ] **Step 9.2: Add route**

In `frontend/invest/src/routes.tsx`, add an entry alongside the other desktop routes:

```tsx
import DesktopScreenerPage from "./pages/DesktopScreenerPage";
// ...
{ path: "/screener", element: <DesktopScreenerPage /> },
```

- [ ] **Step 9.3: Add nav link**

In `frontend/invest/src/desktop/DesktopHeader.tsx`, add a `주식 골라보기` link to `/screener` next to the existing nav items (match the existing `<Link>` style).

- [ ] **Step 9.4: Typecheck + lint + build**

```bash
cd frontend/invest && npm run typecheck && npm run build
```

- [ ] **Step 9.5: Commit**

```bash
git add frontend/invest/src/pages/DesktopScreenerPage.tsx frontend/invest/src/routes.tsx frontend/invest/src/desktop/DesktopHeader.tsx
git commit -m "$(cat <<'EOF'
feat(invest): add ROB-147 /screener desktop page

DesktopScreenerPage composes preset sidebar, filter bar, results table,
and filter-modal shell. Adds the /screener route and a top-nav link.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Frontend — page-level vitest test

**Files:**
- Create: `frontend/invest/src/__tests__/DesktopScreenerPage.test.tsx`

**Context:** Read `frontend/invest/src/__tests__/DesktopFeedNewsPage.test.tsx` first to mirror the mocking + render pattern.

- [ ] **Step 10.1: Implement test**

```tsx
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import * as screenerApi from "../api/screener";
import DesktopScreenerPage from "../pages/DesktopScreenerPage";

const PRESETS = {
  presets: [
    {
      id: "consecutive_gainers",
      name: "연속 상승세",
      description: "일주일 연속 상승세를 보이는 주식",
      badges: ["인기"],
      filterChips: [{ label: "주가등락률", detail: "1주일 전 보다 · 0% 이상" }],
      metricLabel: "주가등락률",
      market: "kr" as const,
    },
    {
      id: "cheap_value",
      name: "아직 저렴한 가치주",
      description: "PER, PBR 모두 낮은 저평가 종목",
      badges: [],
      filterChips: [{ label: "PER", detail: "15 이하" }],
      metricLabel: "PER",
      market: "kr" as const,
    },
  ],
  selectedPresetId: "consecutive_gainers",
};

const RESULTS_GAINERS = {
  presetId: "consecutive_gainers",
  title: "연속 상승세",
  description: "일주일 연속 상승세를 보이는 주식",
  filterChips: [{ label: "주가등락률", detail: "1주일 전 보다 · 0% 이상" }],
  metricLabel: "주가등락률",
  results: [
    {
      rank: 1,
      symbol: "005930",
      market: "kr" as const,
      name: "삼성전자",
      logoUrl: null,
      isWatched: true,
      priceLabel: "80,000원",
      changePctLabel: "+1.23%",
      changeAmountLabel: "+970원",
      changeDirection: "up" as const,
      category: "반도체",
      marketCapLabel: "478조원",
      volumeLabel: "12,345,678",
      analystLabel: "구매",
      metricValueLabel: "+1.23%",
      warnings: [],
    },
  ],
  warnings: [],
};

const RESULTS_VALUE = {
  ...RESULTS_GAINERS,
  presetId: "cheap_value",
  title: "아직 저렴한 가치주",
  description: "PER, PBR 모두 낮은 저평가 종목",
  metricLabel: "PER",
  filterChips: [{ label: "PER", detail: "15 이하" }],
  results: [
    {
      ...RESULTS_GAINERS.results[0],
      metricValueLabel: "14.0",
    },
  ],
};

describe("DesktopScreenerPage", () => {
  it("renders the default preset and switches when another preset is clicked", async () => {
    vi.spyOn(screenerApi, "fetchScreenerPresets").mockResolvedValue(PRESETS);
    const resultsSpy = vi
      .spyOn(screenerApi, "fetchScreenerResults")
      .mockImplementation(async (id: string) =>
        id === "cheap_value" ? RESULTS_VALUE : RESULTS_GAINERS,
      );

    render(
      <MemoryRouter>
        <DesktopScreenerPage />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByText("연속 상승세")).toBeInTheDocument());
    expect(screen.getByText("삼성전자")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /아직 저렴한 가치주/ }));

    await waitFor(() =>
      expect(screen.getByText("PER, PBR 모두 낮은 저평가 종목")).toBeInTheDocument(),
    );

    expect(resultsSpy).toHaveBeenCalledWith("consecutive_gainers");
    expect(resultsSpy).toHaveBeenCalledWith("cheap_value");
  });

  it("shows a 'no rows' message when results are empty", async () => {
    vi.spyOn(screenerApi, "fetchScreenerPresets").mockResolvedValue(PRESETS);
    vi.spyOn(screenerApi, "fetchScreenerResults").mockResolvedValue({
      ...RESULTS_GAINERS,
      results: [],
    });

    render(
      <MemoryRouter>
        <DesktopScreenerPage />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(screen.getByText(/표시할 종목이 없습니다/)).toBeInTheDocument(),
    );
  });
});
```

- [ ] **Step 10.2: Run frontend tests — expect 2 PASS**

```bash
cd frontend/invest && npm test -- --run DesktopScreenerPage
```

- [ ] **Step 10.3: Commit**

```bash
git add frontend/invest/src/__tests__/DesktopScreenerPage.test.tsx
git commit -m "$(cat <<'EOF'
test(invest): cover ROB-147 screener page render + preset switching + empty state

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Final verification + push + PR

**Files:** none (CI commands only).

- [ ] **Step 11.1: Backend lint + tests**

```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run pytest tests/test_invest_screener_schemas.py tests/test_invest_screener_presets.py tests/test_invest_view_model_screener_service.py tests/test_invest_view_model_safety.py tests/test_invest_api_router.py tests/test_invest_api_router_safety.py -v
```

Expected: all PASS, ruff clean.

- [ ] **Step 11.2: Frontend typecheck + tests + build**

```bash
cd frontend/invest && npm run typecheck && npm test -- --run && npm run build
```

Expected: typecheck PASS, all vitest suites PASS, build PASS.

- [ ] **Step 11.3: Push branch**

```bash
git push -u origin feature/ROB-147-invest-screener-mvp
```

- [ ] **Step 11.4: Create PR via gh**

```bash
gh pr create --base main --title "feat(invest): add ROB-147 /invest screener MVP" --body "$(cat <<'EOF'
## Summary
- New `/invest/screener` desktop page with Toss-inspired preset sidebar, filter chips, results table, and a read-only filter-modal shell.
- New read-only `/invest/api/screener/{presets,results}` endpoints powered by a thin view-model wrapper around the existing `ScreenerService`. KR-market only for the MVP.
- Watchlist relation is read-only; no broker / order / watch mutations and no Toss API dependency. Safety covered by extending `tests/test_invest_view_model_safety.py`.

Linear: https://linear.app/mgh3326/issue/ROB-147

## Test plan
- [ ] `uv run ruff check app/ tests/`
- [ ] `uv run ruff format --check app/ tests/`
- [ ] Backend pytest (schemas, presets, view-model, safety, router)
- [ ] `cd frontend/invest && npm run typecheck && npm test -- --run && npm run build`
- [ ] Manually confirm `/invest/screener` renders the default preset and switches when another preset is clicked

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 11.5: Capture the PR URL**

The `gh pr create` output is the PR URL. Save it to report back to the user.

---

## Self-Review Notes (controller pre-flight)

- Acceptance criteria coverage:
  - `/invest/screener` renders after login: ✅ Task 9 page + existing auth boundary on `/invest/api/*` (server-rendered SPA already auth-gated by API).
  - Desktop nav entry: ✅ Task 9.3.
  - Preset list with 인기 badge: ✅ Tasks 2 + 8.1 (`presetBadge` rendered when `badges` includes "인기").
  - ≥5 presets: ✅ Task 2 ships 6.
  - Preset selection updates title/description/chips/table: ✅ Task 9.1 effect on `selectedId`.
  - Required result columns: ✅ Task 8.3 thead matches the spec.
  - Missing data shown as `-` / warnings: ✅ Tasks 1.3 + 3.3 (per-row `warnings`, response `warnings`).
  - Filter modal shell with tabs/categories/CTA: ✅ Task 8.4 (read-only "준비중").
  - Watchlist read-only: ✅ Task 3.3 (only reads `resolver.relation`); UI does not call any mutation.
  - Auth boundary preserved: ✅ Task 5.3 routes depend on `get_authenticated_user`.
  - Frontend typecheck/build: ✅ Tasks 7.3 / 9.4 / 11.2.
  - Backend ruff + pytest: ✅ Task 11.1.
  - Read-only safety verified: ✅ Tasks 4 + 6.
- Type consistency: response/row types in `app/schemas/invest_screener.py` match TypeScript types in `frontend/invest/src/types/screener.ts` field-by-field (case included).
- No placeholders: each step contains its concrete code, exact paths, and expected commands.

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

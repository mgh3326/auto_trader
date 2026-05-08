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

_VALID_MARKETS = {"kr", "us", "crypto"}
_KR_ABSURD_MARKET_CAP_KRW = 10_000_000_000_000_000


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


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_market(raw: Any) -> str:
    market = _clean_text(raw).lower()
    return market if market in _VALID_MARKETS else "kr"


def _normalize_symbol(row: dict[str, Any], market: str) -> tuple[str, list[str]]:
    symbol = ""
    for key in ("symbol", "code", "short_code", "ticker"):
        symbol = _clean_text(row.get(key))
        if symbol:
            break

    if not symbol:
        return "", ["종목코드 데이터 준비중"]

    if market == "kr":
        _, sep, suffix = symbol.rpartition(":")
        if sep and suffix.isdigit() and len(suffix) == 6:
            symbol = suffix
        return symbol, []

    return symbol.upper(), []


def _coerce_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _market_cap_from_market_cap_field(value: float | None, market: str) -> float | None:
    if value is None or value <= 0:
        return None
    if value >= 1_000_000_000_000:
        return value
    if market == "kr":
        return value * 100_000_000
    return None


def _normalize_market_cap_krw(
    row: dict[str, Any], market: str
) -> tuple[float | None, list[str]]:
    """Return display-safe KRW market cap plus row warnings.

    Upstream screener rows can mix `market_cap_krw` (already KRW) with
    source-dependent `market_cap` units. Values above 10,000조 KRW are
    implausible for a single screener row, so prefer a plausible fallback or
    hide the label instead of rendering absurd values such as 414,671.4조원.
    """
    market_cap_krw = _coerce_float(row.get("market_cap_krw"))
    market_cap = _coerce_float(row.get("market_cap"))
    fallback = _market_cap_from_market_cap_field(market_cap, market)

    if market_cap_krw is not None and market_cap_krw > 0:
        if market_cap_krw <= _KR_ABSURD_MARKET_CAP_KRW:
            return market_cap_krw, []
        if fallback is not None and fallback <= _KR_ABSURD_MARKET_CAP_KRW:
            return fallback, ["시가총액 단위 보정됨"]
        return None, ["시가총액 데이터 확인 필요"]

    if fallback is not None and fallback <= _KR_ABSURD_MARKET_CAP_KRW:
        return fallback, []
    if fallback is not None:
        return None, ["시가총액 데이터 확인 필요"]
    return None, []


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
    rows: list[dict[str, Any]] = list(raw.get("results") or raw.get("stocks") or [])
    upstream_warnings: list[str] = list(raw.get("warnings") or [])

    results: list[ScreenerResultRow] = []
    for idx, row in enumerate(rows, start=1):
        market = _normalize_market(row.get("market"))
        symbol, symbol_warnings = _normalize_symbol(row, market)
        market_cap, market_cap_warnings = _normalize_market_cap_krw(row, market)
        change_pct_label, direction = _format_change_pct(row.get("change_rate"))
        metric_label, metric_warnings = _metric_value_label(preset_id, row)
        relation = resolver.relation(market, symbol)
        is_watched = relation in ("watchlist", "both")
        row_warnings = symbol_warnings + market_cap_warnings + metric_warnings
        results.append(
            ScreenerResultRow(
                rank=idx,
                symbol=symbol,
                market=market,  # type: ignore[arg-type]
                name=_clean_text(row.get("name")) or symbol,
                logoUrl=row.get("logo_url"),
                isWatched=is_watched,
                priceLabel=_format_price(row.get("close") or row.get("price")),
                changePctLabel=change_pct_label,
                changeAmountLabel=_format_change_amount(row.get("change_amount")),
                changeDirection=direction,
                category=str(row.get("sector") or row.get("category") or "-"),
                marketCapLabel=_format_market_cap_kr(market_cap),
                volumeLabel=_format_volume(row.get("volume")),
                analystLabel=str(row.get("analyst_label") or "-"),
                metricValueLabel=metric_label,
                warnings=row_warnings,
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

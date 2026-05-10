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

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from datetime import time as _time
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.invest_screener import (
    ChangeDirection,
    ScreenerFreshness,
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

_KST = ZoneInfo("Asia/Seoul")
_KR_OPEN = _time(9, 0)
_KR_CLOSE = _time(15, 30)
_CACHE_HIT_FRESH_SECONDS = 300


class _ScreeningServiceProto(Protocol):
    async def list_screening(self, /, **kwargs: Any) -> dict[str, Any]: ...


class _ResolverProto(Protocol):
    def relation(self, market: str, symbol: str) -> str: ...


def build_screener_presets() -> ScreenerPresetsResponse:
    return ScreenerPresetsResponse(
        presets=preset_definitions("kr"),
        selectedPresetId=DEFAULT_PRESET_ID,
    )


_METRIC_FIELD: dict[str, str] = {
    "consecutive_gainers": "consecutive_up_days",
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


def _format_change_amount(amount: float | None, market: str = "kr") -> str:
    if amount is None:
        return "-"
    sign = "+" if amount > 0 else "-" if amount < 0 else ""
    if market == "us":
        return f"{sign}${abs(float(amount)):,.2f}"
    return f"{sign}{abs(int(amount)):,}원"


def _format_price(close: float | None, market: str = "kr") -> str:
    if close is None:
        return "-"
    if market == "us":
        return f"${float(close):,.2f}"
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
    if market == "kr":
        # KR upstream rows can contain either KRW (TradingView-style) or 억원
        # (KRX-style). A KRW market cap under 1조 is still plausible, so don't
        # require a 1조 threshold before treating the value as already-KRW.
        if value >= 100_000_000:
            return value
        return value * 100_000_000
    if value >= 1_000_000_000_000:
        return value
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


def _format_market_cap_us(market_cap: float | None) -> str:
    if market_cap is None or market_cap <= 0:
        return "-"
    if market_cap >= 1_000_000_000_000:
        return f"${market_cap / 1_000_000_000_000:.2f}T"
    if market_cap >= 1_000_000_000:
        return f"${market_cap / 1_000_000_000:.1f}B"
    if market_cap >= 1_000_000:
        return f"${market_cap / 1_000_000:.1f}M"
    return f"${market_cap:,.0f}"


def _format_market_cap(row: dict[str, Any], market: str) -> tuple[str, list[str]]:
    if market == "us":
        market_cap = _coerce_float(row.get("market_cap_usd"))
        if market_cap is None:
            market_cap = _coerce_float(row.get("market_cap"))
        return _format_market_cap_us(market_cap), []
    market_cap, warnings = _normalize_market_cap_krw(row, market)
    return _format_market_cap_kr(market_cap), warnings


def _format_volume(volume: float | None) -> str:
    if volume is None:
        return "-"
    return f"{int(volume):,}"


def calculate_consecutive_up_days(closes: Sequence[float | int | None]) -> int | None:
    values = [_coerce_float(v) for v in closes]
    values = [v for v in values if v is not None]
    if len(values) < 2:
        return None
    streak = 0
    for current, previous in zip(
        reversed(values[1:]), reversed(values[:-1]), strict=False
    ):
        if current > previous:
            streak += 1
            continue
        break
    return streak


def _enrich_consecutive_up_days(preset_id: str, row: dict[str, Any]) -> None:
    if preset_id != "consecutive_gainers" or row.get("consecutive_up_days") is not None:
        return
    history = row.get("daily_closes") or row.get("close_history") or row.get("closes")
    if isinstance(history, Sequence) and not isinstance(history, (str, bytes)):
        row["consecutive_up_days"] = calculate_consecutive_up_days(history)


def _metric_value_label(preset_id: str, row: dict[str, Any]) -> tuple[str, list[str]]:
    field = _METRIC_FIELD.get(preset_id)
    if not field:
        return "-", []
    value = row.get(field)
    if value is None:
        if field == "consecutive_up_days":
            return "-", ["연속상승 데이터 준비중"]
        return "-", [f"{field.upper()} 데이터 준비중"]
    if field == "consecutive_up_days":
        return f"{int(value)}일", []
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


def _format_relative_korean(delta_seconds: int) -> str:
    if delta_seconds <= 60:
        return "방금 갱신"
    minutes = delta_seconds // 60
    if minutes < 60:
        return f"{minutes}분 전 갱신"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}시간 전 갱신"
    days = hours // 24
    return f"{days}일 전 갱신"


def _is_kr_market_open(at_kst: datetime) -> bool:
    if at_kst.weekday() >= 5:
        return False
    return _KR_OPEN <= at_kst.time() <= _KR_CLOSE


def _build_freshness(
    *,
    raw_timestamp: str | None,
    cache_hit: bool,
    market: str,
    now: Callable[[], datetime],
    dataState: str = "missing",
) -> ScreenerFreshness:
    now_utc = now()
    if not raw_timestamp:
        fetched = now_utc
    else:
        try:
            fetched = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        except ValueError:
            fetched = now_utc
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=UTC)
    fetched_kst = fetched.astimezone(_KST)
    now_kst = now_utc.astimezone(_KST)
    delta = max(0, int((now_utc - fetched).total_seconds()))

    market_open = market == "kr" and _is_kr_market_open(now_kst)
    if not market_open and delta > _CACHE_HIT_FRESH_SECONDS * 4:
        source: Literal["live", "cached", "previous_session"] = "previous_session"
        relative = "전 거래일 기준"
    elif cache_hit:
        source = "cached"
        relative = _format_relative_korean(delta)
    else:
        source = "live"
        relative = _format_relative_korean(delta)

    return ScreenerFreshness(
        fetchedAt=fetched.astimezone(UTC).isoformat(),
        asOfLabel=fetched_kst.strftime("%Y.%m.%d %H:%M 기준"),
        relativeLabel=relative,
        cacheHit=bool(cache_hit),
        source=source,
        dataState=dataState,  # type: ignore[arg-type]
    )


async def build_screener_results(
    preset_id: str,
    screening_service: _ScreeningServiceProto,
    resolver: _ResolverProto,
    market: str = "kr",
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    session: AsyncSession | None = None,
) -> ScreenerResultsResponse:
    requested_market = "us" if market == "us" else "kr"
    preset = get_preset(preset_id, requested_market)
    if preset is None:
        freshness = _build_freshness(
            raw_timestamp=None,
            cache_hit=False,
            market=requested_market,
            now=now,
        )
        return ScreenerResultsResponse(
            presetId=preset_id,
            title=preset_id,
            description="",
            filterChips=[],
            metricLabel="-",
            results=[],
            warnings=[f"알 수 없는 프리셋: {preset_id}"],
            freshness=freshness,
        )

    filters = screening_filters_for(preset_id, requested_market)
    raw = await screening_service.list_screening(**filters)
    rows: list[dict[str, Any]] = list(raw.get("results") or raw.get("stocks") or [])
    upstream_warnings: list[str] = list(raw.get("warnings") or [])

    # ROB-170 follow-up: snapshot-first hydration runs at the view-model layer so
    # the session reaches _enrich_consecutive_up_days. Without this call the
    # screening service path never sees the session and _screener_snapshot_state
    # is never populated, leaving dataState pinned at "missing".
    if (
        session is not None
        and requested_market in {"kr", "us"}
        and preset_id == "consecutive_gainers"
        and rows
    ):
        from app.mcp_server.tooling.screening.enrichment import (
            _enrich_consecutive_up_days as _async_enrich,
        )

        await _async_enrich(rows, market=requested_market, session=session)

    # Aggregate snapshot dataState from enriched rows (set by _enrich_consecutive_up_days when session provided)
    from app.services.invest_screener_snapshots.freshness import aggregate_states

    _row_states: list[str] = [
        str(r.get("_screener_snapshot_state") or "missing") for r in rows
    ]
    _aggregated_data_state = aggregate_states(_row_states)  # type: ignore[arg-type]

    freshness = _build_freshness(
        raw_timestamp=raw.get("timestamp"),
        cache_hit=bool(raw.get("cache_hit")),
        market=requested_market,
        now=now,
        dataState=_aggregated_data_state,
    )

    # Bulk-lookup Korean names for KR rows from kr_symbol_universe
    _kr_names: dict[str, str] = {}
    if session is not None and requested_market == "kr" and rows:
        import sqlalchemy as sa

        from app.models.kr_symbol_universe import KRSymbolUniverse

        kr_symbols = [
            _normalize_symbol(r, "kr")[0]
            for r in rows
            if _normalize_market(r.get("market") or requested_market) == "kr"
        ]
        if kr_symbols:
            _kr_result = await session.execute(
                sa.select(KRSymbolUniverse.symbol, KRSymbolUniverse.name).where(
                    KRSymbolUniverse.symbol.in_(kr_symbols)
                )
            )
            _kr_names = {row_t.symbol: row_t.name for row_t in _kr_result.all()}

    results: list[ScreenerResultRow] = []
    for idx, row in enumerate(rows, start=1):
        market = _normalize_market(row.get("market") or requested_market)
        symbol, symbol_warnings = _normalize_symbol(row, market)
        market_cap_label, market_cap_warnings = _format_market_cap(row, market)
        change_pct_label, direction = _format_change_pct(row.get("change_rate"))
        _enrich_consecutive_up_days(preset_id, row)
        metric_label, metric_warnings = _metric_value_label(preset_id, row)
        relation = resolver.relation(market, symbol)
        is_watched = relation in ("watchlist", "both")
        row_warnings = symbol_warnings + market_cap_warnings + metric_warnings
        results.append(
            ScreenerResultRow(
                rank=idx,
                symbol=symbol,
                market=market,  # type: ignore[arg-type]
                name=_kr_names.get(symbol) or _clean_text(row.get("name")) or symbol,
                logoUrl=row.get("logo_url"),
                isWatched=is_watched,
                priceLabel=_format_price(
                    row.get("close") or row.get("price") or row.get("current_price"),
                    market,
                ),
                changePctLabel=change_pct_label,
                changeAmountLabel=_format_change_amount(
                    row.get("change_amount"), market
                ),
                changeDirection=direction,
                category=str(row.get("sector") or row.get("category") or "-"),
                marketCapLabel=market_cap_label,
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
        freshness=freshness,
    )

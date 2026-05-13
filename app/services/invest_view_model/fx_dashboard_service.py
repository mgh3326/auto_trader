"""Read-only provider-backed service for /invest FX·macro dashboard."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

from app.schemas.invest_fx_dashboard import (
    FxDashboardAfterVerification,
    FxDashboardCollectionItem,
    FxDashboardDataState,
    FxDashboardDisclaimer,
    FxDashboardEventsSection,
    FxDashboardEvidenceItem,
    FxDashboardForeignFlowSection,
    FxDashboardNewsSection,
    FxDashboardQuoteMetric,
    FxDashboardResponse,
    FxDashboardSourceFreshness,
    FxDashboardThreshold,
)
from app.services.invest_view_model.fx_defense_signal import (
    DefenseScoringInput,
    _score_defense_signal,
    _threshold_state,
)

NAVER_MARKETINDEX_URL = "https://m.stock.naver.com/front-api/marketIndex/productDetail"
NAVER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://m.stock.naver.com/marketindex/exchange/FX_USDKRW",
    "Accept": "application/json, text/plain, */*",
}
USD_KRW_STALE_AFTER_MINUTES = 10
KRW_CROSSES_STALE_AFTER_MINUTES = 10
GLOBAL_DOLLAR_STALE_AFTER_MINUTES = 60
FALLBACK_USDKRW_SPOT = 1498.70


@dataclass(frozen=True)
class FxProviderQuote:
    """Normalized read-only FX quote produced by a dashboard provider."""

    symbol: str
    label: str
    value: float | None
    change: float | None = None
    change_pct: float | None = None
    updated_at: datetime | None = None
    source: str = "unknown"
    data_state: FxDashboardDataState = "fresh"
    warning: str | None = None


class FxDashboardProvider(Protocol):
    """Read-only FX dashboard provider contract."""

    async def get_usdkrw(self, *, as_of: datetime) -> FxProviderQuote: ...

    async def get_global_dollar(self, *, as_of: datetime) -> list[FxProviderQuote]: ...

    async def get_krw_crosses(self, *, as_of: datetime) -> list[FxProviderQuote]: ...


@dataclass(frozen=True)
class _NaverMarketIndexConfig:
    code: str
    symbol: str
    label: str
    source: str
    stale_after_minutes: int


@dataclass(frozen=True)
class _YahooGlobalConfig:
    symbol: str
    label: str
    tickers: tuple[str, ...]


NAVER_USDKRW = _NaverMarketIndexConfig(
    code="FX_USDKRW",
    symbol="USDKRW",
    label="USD/KRW 현물",
    source="naver_marketindex_usdkrw",
    stale_after_minutes=USD_KRW_STALE_AFTER_MINUTES,
)
NAVER_KRW_CROSSES = (
    _NaverMarketIndexConfig(
        code="FX_CNYKRW",
        symbol="CNYKRW",
        label="위안/원",
        source="naver_marketindex_krw_crosses",
        stale_after_minutes=KRW_CROSSES_STALE_AFTER_MINUTES,
    ),
    _NaverMarketIndexConfig(
        code="FX_JPYKRW",
        symbol="JPYKRW",
        label="엔/원",
        source="naver_marketindex_krw_crosses",
        stale_after_minutes=KRW_CROSSES_STALE_AFTER_MINUTES,
    ),
    _NaverMarketIndexConfig(
        code="FX_EURKRW",
        symbol="EURKRW",
        label="유로/원",
        source="naver_marketindex_krw_crosses",
        stale_after_minutes=KRW_CROSSES_STALE_AFTER_MINUTES,
    ),
)
YAHOO_GLOBAL_DOLLAR = (
    _YahooGlobalConfig(
        symbol="DXY",
        label="달러인덱스 proxy",
        tickers=("DX-Y.NYB", "DX=F"),
    ),
    _YahooGlobalConfig(
        symbol="USDCNH",
        label="달러/위안 offshore",
        tickers=("CNH=X", "USDCNH=X"),
    ),
    _YahooGlobalConfig(symbol="USDJPY", label="달러/엔", tickers=("JPY=X",)),
    _YahooGlobalConfig(symbol="EURUSD", label="유로/달러", tickers=("EURUSD=X",)),
)


def _now() -> datetime:
    return datetime.now(UTC)


def _coerce_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _distance_pct(*, level: float, spot: float) -> float:
    return round(((level - spot) / spot) * 100, 2)


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if not value:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return _coerce_aware(parsed)


def _mapping_or_attr_get(value: Any, *keys: str) -> Any:
    for key in keys:
        item = getattr(value, key, None)
        if item is None and hasattr(value, "get"):
            try:
                item = value.get(key)
            except Exception:
                item = None
        if item is not None:
            return item
    return None


def _fetch_yahoo_fast_info_raw_sync(ticker: str) -> dict[str, Any]:
    """Fetch Yahoo fast_info for an already-Yahoo-formatted ticker.

    This seam intentionally does not import the shared Yahoo broker client because
    that module normalizes app symbols through ``to_yahoo_symbol``. FX/index
    Yahoo symbols such as ``DX-Y.NYB`` are already in Yahoo format and must not be
    transformed to ``DX-Y-NYB``.
    """
    import yfinance as yf

    info = yf.Ticker(ticker).fast_info
    return {
        "symbol": ticker,
        "previous_close": _parse_float(
            _mapping_or_attr_get(
                info,
                "regular_market_previous_close",
                "regularMarketPreviousClose",
                "previous_close",
                "previousClose",
            )
        ),
        "open": _parse_float(_mapping_or_attr_get(info, "open")),
        "high": _parse_float(_mapping_or_attr_get(info, "day_high", "dayHigh")),
        "low": _parse_float(_mapping_or_attr_get(info, "day_low", "dayLow")),
        "close": _parse_float(
            _mapping_or_attr_get(
                info,
                "last_price",
                "lastPrice",
                "regular_market_price",
                "regularMarketPrice",
            )
        ),
    }


async def _fetch_yahoo_fast_info_raw(ticker: str) -> dict[str, Any]:
    return await asyncio.to_thread(_fetch_yahoo_fast_info_raw_sync, ticker)


def _quote_state_from_updated_at(
    *,
    updated_at: datetime | None,
    as_of: datetime,
    stale_after_minutes: int,
) -> FxDashboardDataState:
    if updated_at is None:
        return "partial"
    age = _coerce_aware(as_of) - _coerce_aware(updated_at)
    if age > timedelta(minutes=stale_after_minutes):
        return "stale"
    return "fresh"


def _tone_from_change_pct(change_pct: float | None) -> str:
    if change_pct is None:
        return "unknown"
    if change_pct > 0:
        return "up"
    if change_pct < 0:
        return "down"
    return "flat"


def _source_state(states: Iterable[FxDashboardDataState]) -> FxDashboardDataState:
    ordered = list(states)
    if not ordered:
        return "missing"
    if all(state == "fresh" for state in ordered):
        return "fresh"
    if all(state == "missing" for state in ordered):
        return "missing"
    if all(state == "stale" for state in ordered):
        return "stale"
    if all(state == "error" for state in ordered):
        return "error"
    if any(state == "error" for state in ordered):
        return "partial"
    return "partial"


def _collection_item_from_quote(quote: FxProviderQuote) -> FxDashboardCollectionItem:
    return FxDashboardCollectionItem(
        symbol=quote.symbol,
        label=quote.label,
        value=quote.value,
        changePct=quote.change_pct,
        dataState=quote.data_state,
        source=quote.source,
    )


def _missing_quote(
    *,
    symbol: str,
    label: str,
    source: str,
    warning: str | None = None,
    data_state: FxDashboardDataState = "missing",
) -> FxProviderQuote:
    return FxProviderQuote(
        symbol=symbol,
        label=label,
        value=None,
        source=source,
        data_state=data_state,
        warning=warning,
    )


async def _fetch_naver_marketindex_quote(
    config: _NaverMarketIndexConfig,
    *,
    as_of: datetime,
    client: httpx.AsyncClient | None = None,
) -> FxProviderQuote:
    owns_client = client is None
    resolved_client = client or httpx.AsyncClient(timeout=10, headers=NAVER_HEADERS)
    try:
        response = await resolved_client.get(
            NAVER_MARKETINDEX_URL,
            params={"category": "exchange", "reutersCode": config.code},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("isSuccess") is False:
            raise ValueError("Naver marketIndex returned unsuccessful status")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ValueError("Naver marketIndex returned empty result")

        value = _parse_float(result.get("closePrice"))
        if value is None:
            raise ValueError("Naver marketIndex quote missing closePrice")
        change = _parse_float(result.get("compareToPreviousClosePrice"))
        change_pct = _parse_float(result.get("fluctuationsRatio"))
        updated_at = _parse_datetime(result.get("localTradedAt"))
        data_state = _quote_state_from_updated_at(
            updated_at=updated_at,
            as_of=as_of,
            stale_after_minutes=config.stale_after_minutes,
        )
        warning = None
        if data_state == "stale":
            warning = f"{config.source}: provider timestamp is stale"
        elif data_state == "partial":
            warning = f"{config.source}: provider timestamp unavailable"
        return FxProviderQuote(
            symbol=config.symbol,
            label=config.label,
            value=value,
            change=change,
            change_pct=change_pct,
            updated_at=updated_at,
            source=config.source,
            data_state=data_state,
            warning=warning,
        )
    finally:
        if owns_client:
            await resolved_client.aclose()


def _normalize_yahoo_fast_info(
    *,
    config: _YahooGlobalConfig,
    ticker: str,
    fast_info: dict[str, Any],
    as_of: datetime,
) -> FxProviderQuote:
    close = _parse_float(fast_info.get("close"))
    previous_close = _parse_float(fast_info.get("previous_close"))
    if close is None:
        return _missing_quote(
            symbol=config.symbol,
            label=config.label,
            source="yahoo_global_dollar",
            warning=f"yahoo_global_dollar: {config.symbol} unavailable from {ticker}",
        )
    change = None
    change_pct = None
    if previous_close not in (None, 0):
        change = round(close - previous_close, 6)
        change_pct = round((change / previous_close) * 100, 4)
    return FxProviderQuote(
        symbol=config.symbol,
        label=config.label,
        value=close,
        change=change,
        change_pct=change_pct,
        updated_at=as_of,
        source="yahoo_global_dollar",
        data_state="fresh",
        warning=None
        if change_pct is not None
        else f"yahoo_global_dollar: {config.symbol} precise change unavailable",
    )


class DefaultFxDashboardProvider:
    """Default request-time provider for the read-only FX dashboard."""

    def __init__(
        self,
        *,
        yahoo_fast_info_fetcher: Any | None = None,
    ) -> None:
        self._yahoo_fast_info_fetcher = (
            yahoo_fast_info_fetcher or _fetch_yahoo_fast_info_raw
        )

    async def get_usdkrw(self, *, as_of: datetime) -> FxProviderQuote:
        return await _fetch_naver_marketindex_quote(NAVER_USDKRW, as_of=as_of)

    async def get_global_dollar(self, *, as_of: datetime) -> list[FxProviderQuote]:
        return await asyncio.gather(
            *(
                self._fetch_yahoo_quote(config, as_of=as_of)
                for config in YAHOO_GLOBAL_DOLLAR
            )
        )

    async def get_krw_crosses(self, *, as_of: datetime) -> list[FxProviderQuote]:
        async with httpx.AsyncClient(timeout=10, headers=NAVER_HEADERS) as client:
            results = await asyncio.gather(
                *(
                    _fetch_naver_marketindex_quote(
                        config,
                        as_of=as_of,
                        client=client,
                    )
                    for config in NAVER_KRW_CROSSES
                ),
                return_exceptions=True,
            )
        quotes: list[FxProviderQuote] = []
        for config, result in zip(NAVER_KRW_CROSSES, results, strict=True):
            if isinstance(result, Exception):
                quotes.append(
                    _missing_quote(
                        symbol=config.symbol,
                        label=config.label,
                        source=config.source,
                        warning=f"{config.source}: {config.symbol} unavailable",
                        data_state="error",
                    )
                )
            else:
                quotes.append(result)
        return quotes

    async def _fetch_yahoo_quote(
        self,
        config: _YahooGlobalConfig,
        *,
        as_of: datetime,
    ) -> FxProviderQuote:
        warnings: list[str] = []
        for ticker in config.tickers:
            try:
                fast_info = await self._yahoo_fast_info_fetcher(ticker)
            except Exception:
                warnings.append(f"{ticker} unavailable")
                continue
            quote = _normalize_yahoo_fast_info(
                config=config,
                ticker=ticker,
                fast_info=fast_info,
                as_of=as_of,
            )
            if quote.value is not None:
                return quote
            if quote.warning:
                warnings.append(quote.warning)
        return _missing_quote(
            symbol=config.symbol,
            label=config.label,
            source="yahoo_global_dollar",
            warning=f"yahoo_global_dollar: {config.symbol} unavailable"
            + (f" ({'; '.join(warnings)})" if warnings else ""),
        )


def _fallback_usdkrw_quote(*, as_of: datetime, warning: str) -> FxProviderQuote:
    updated_at = as_of - timedelta(hours=12)
    return FxProviderQuote(
        symbol="USDKRW",
        label="USD/KRW 현물",
        value=FALLBACK_USDKRW_SPOT,
        change=None,
        change_pct=None,
        updated_at=updated_at,
        source="fixture_usdkrw_spot_fallback",
        data_state="stale",
        warning=warning,
    )


def _missing_global_dollar_quotes(
    *, data_state: FxDashboardDataState = "missing"
) -> list[FxProviderQuote]:
    return [
        _missing_quote(
            symbol=config.symbol,
            label=config.label,
            source="yahoo_global_dollar",
            warning=f"yahoo_global_dollar: {config.symbol} unavailable",
            data_state=data_state,
        )
        for config in YAHOO_GLOBAL_DOLLAR
    ]


def _missing_krw_cross_quotes(
    *, data_state: FxDashboardDataState = "missing"
) -> list[FxProviderQuote]:
    return [
        _missing_quote(
            symbol=config.symbol,
            label=config.label,
            source=config.source,
            warning=f"{config.source}: {config.symbol} unavailable",
            data_state=data_state,
        )
        for config in NAVER_KRW_CROSSES
    ]


def _freshness_for_source(
    *,
    source: str,
    label: str,
    quotes: list[FxProviderQuote],
    stale_after_minutes: int | None,
    warning: str | None = None,
) -> FxDashboardSourceFreshness:
    source_quotes = [quote for quote in quotes if quote.source == source]
    source_warning = (
        warning
        or "; ".join(quote.warning for quote in source_quotes if quote.warning)
        or None
    )
    updated_values = [quote.updated_at for quote in source_quotes if quote.updated_at]
    return FxDashboardSourceFreshness(
        source=source,
        label=label,
        dataState=_source_state(quote.data_state for quote in source_quotes),
        updatedAt=max(updated_values) if updated_values else None,
        staleAfterMinutes=stale_after_minutes,
        warning=source_warning,
    )


async def build_fx_dashboard(
    *,
    as_of: datetime | None = None,
    provider: FxDashboardProvider | None = None,
) -> FxDashboardResponse:
    """Build a read-only FX dashboard with provider degradation.

    The service performs request-time quote reads only. It does not import broker
    order services, create watch/order intents, write to databases, run backfills,
    or activate schedulers.
    """
    resolved_as_of = _coerce_aware(as_of or _now())
    resolved_provider = provider or DefaultFxDashboardProvider()

    usd_result, global_result, crosses_result = await asyncio.gather(
        resolved_provider.get_usdkrw(as_of=resolved_as_of),
        resolved_provider.get_global_dollar(as_of=resolved_as_of),
        resolved_provider.get_krw_crosses(as_of=resolved_as_of),
        return_exceptions=True,
    )

    warnings: list[str] = []
    if isinstance(usd_result, Exception):
        usd_quote = _fallback_usdkrw_quote(
            as_of=resolved_as_of,
            warning="naver_marketindex_usdkrw: provider unavailable; stale fixture fallback used",
        )
        warnings.append(usd_quote.warning or "naver_marketindex_usdkrw unavailable")
    else:
        usd_quote = usd_result
        if usd_quote.warning:
            warnings.append(usd_quote.warning)

    if isinstance(global_result, Exception):
        global_quotes = _missing_global_dollar_quotes(data_state="error")
        warnings.append("yahoo_global_dollar: provider unavailable")
    else:
        global_quotes = global_result
        warnings.extend(quote.warning for quote in global_quotes if quote.warning)

    if isinstance(crosses_result, Exception):
        krw_cross_quotes = _missing_krw_cross_quotes(data_state="error")
        warnings.append("naver_marketindex_krw_crosses: provider unavailable")
    else:
        krw_cross_quotes = crosses_result
        warnings.extend(quote.warning for quote in krw_cross_quotes if quote.warning)

    warnings.append(
        "NDF/flow live providers remain unavailable; news/calendar sections stay read-only/missing"
    )

    spot = usd_quote.value or FALLBACK_USDKRW_SPOT
    freshness = [
        FxDashboardSourceFreshness(
            source=usd_quote.source,
            label="USD/KRW 현물",
            dataState=usd_quote.data_state,
            updatedAt=usd_quote.updated_at,
            staleAfterMinutes=USD_KRW_STALE_AFTER_MINUTES,
            warning=usd_quote.warning,
        ),
        _freshness_for_source(
            source="yahoo_global_dollar",
            label="글로벌 달러 비교",
            quotes=global_quotes,
            stale_after_minutes=GLOBAL_DOLLAR_STALE_AFTER_MINUTES,
        ),
        _freshness_for_source(
            source="naver_marketindex_krw_crosses",
            label="KRW 교차환율",
            quotes=krw_cross_quotes,
            stale_after_minutes=KRW_CROSSES_STALE_AFTER_MINUTES,
        ),
        FxDashboardSourceFreshness(
            source="official_after_verification",
            label="사후 검증 자료",
            dataState="missing",
            updatedAt=None,
            staleAfterMinutes=None,
            warning="공식/딜러/NDF 근거가 없으면 확정 표현 금지",
        ),
    ]

    global_quote_by_symbol = {quote.symbol: quote for quote in global_quotes}
    krw_cross_change_pcts = {
        quote.symbol: quote.change_pct for quote in krw_cross_quotes
    }

    def _global_change_pct(symbol: str) -> float | None:
        quote = global_quote_by_symbol.get(symbol)
        return quote.change_pct if quote is not None else None

    news_context = [
        FxDashboardEvidenceItem(
            kind="news_context",
            labelKo="환율/당국 경계 뉴스 context-only fixture",
            value="1500원 경계감 관련 보도는 참고 맥락으로만 사용",
            source="fixture_fx_news_context",
            dataState="stale",
        )
    ]

    defense_signal = _score_defense_signal(
        DefenseScoringInput(
            spot=spot,
            recent_high=1499.80,
            recent_close_or_last=spot,
            rejected_within_minutes=30,
            global_dollar_change_pct=_global_change_pct("DXY"),
            usdcnh_change_pct=_global_change_pct("USDCNH"),
            usd_jpy_change_pct=_global_change_pct("USDJPY"),
            krw_cross_change_pcts=krw_cross_change_pcts,
            news_context=news_context,
            authority_context=[],
            after_verification_has_strong_evidence=False,
        )
    )

    return FxDashboardResponse(
        asOf=resolved_as_of,
        dataState="partial",
        warnings=warnings,
        disclaimers=[
            FxDashboardDisclaimer(
                code="not_confirmed_intervention",
                severity="caution",
                textKo="이 신호는 방어성 매도/수급 의심을 정리한 참고 지표이며 당국의 확정 개입 근거가 아닙니다. 공식 발표·딜러 코멘트·NDF 등 사후 검증이 필요합니다.",
            )
        ],
        sourceFreshness=freshness,
        usdKrw=FxDashboardQuoteMetric(
            symbol="USDKRW",
            label="USD/KRW 현물",
            value=spot,
            spot=spot,
            change=usd_quote.change,
            changePct=usd_quote.change_pct,
            tone=_tone_from_change_pct(usd_quote.change_pct),
            updatedAt=usd_quote.updated_at,
            dataState=usd_quote.data_state,
            source=usd_quote.source,
        ),
        thresholds=[
            FxDashboardThreshold(
                level=1450,
                label="주의",
                distancePct=_distance_pct(level=1450, spot=spot),
                state="watch",
            ),
            FxDashboardThreshold(
                level=1500,
                label="심리적 저항/당국 경계",
                distancePct=_distance_pct(level=1500, spot=spot),
                state=_threshold_state(level=1500, spot=spot),
            ),
        ],
        defenseSignal=defense_signal,
        globalDollar=[_collection_item_from_quote(quote) for quote in global_quotes],
        krwCrosses=[_collection_item_from_quote(quote) for quote in krw_cross_quotes],
        foreignFlow=FxDashboardForeignFlowSection(
            dataState="missing",
            summaryKo="외국인 수급 연결은 후속 작업입니다.",
            items=[],
        ),
        news=FxDashboardNewsSection(
            dataState="missing",
            items=[],
            warning="FX/당국 발언 뉴스 필터는 ROB-220에서 연결",
        ),
        events=FxDashboardEventsSection(
            dataState="missing",
            items=[],
            warning="FX macro calendar linkage는 ROB-220에서 연결",
        ),
        afterVerification=FxDashboardAfterVerification(
            dataState="missing",
            officialEvidence=[],
            dealerEvidence=[],
            ndfEvidence=[],
            summaryKo="공식 발표·딜러 코멘트·NDF 근거가 확인되기 전까지 확정 개입으로 표현하지 않습니다.",
        ),
    )

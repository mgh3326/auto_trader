"""ROB-251 — read-only market parity view model for /invest.

The default implementation intentionally avoids unapproved production sources:
- no raoni.xyz calls
- no Hyperliquid calls until an explicit collector/source approval exists
- no DB writes, scheduler activation, broker/order/watch imports
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Protocol

from app.mcp_server.tooling.fundamentals._crypto import handle_get_kimchi_premium
from app.mcp_server.tooling.fundamentals._market_index import (
    handle_get_market_index_current_only,
)
from app.schemas.invest_market_parity import (
    InvestMarketParityCard,
    InvestMarketParityResponse,
    InvestParitySource,
    InvestParityState,
    InvestParityTone,
)

_INDEX_PARITY_FORMULA = "((proxyPrice * fxRate * divisor) / basePrice - 1) * 100"
_STABLECOIN_FX_FORMULA = "(usdtKrw / usdKrw - 1) * 100"
_SYNTHETIC_FORMULA = "((syntheticPrice * fxRate) / basePrice - 1) * 100"


@dataclass(frozen=True)
class ParityQuote:
    symbol: str
    price: Decimal
    source: str
    as_of: datetime | None = None
    stale: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class IndexParityConfig:
    id: str
    title: str
    base_symbol: str
    proxy_symbol: str
    divisor: Decimal = Decimal("1")


@dataclass(frozen=True)
class SyntheticParityConfig:
    base_symbol: str
    base_name: str
    synthetic_symbol: str
    title: str


class MarketParityProvider(Protocol):
    async def get_index_quote(self, symbol: str) -> ParityQuote | None: ...
    async def get_proxy_quote(self, symbol: str) -> ParityQuote | None: ...
    async def get_fx_rate(self, pair: str) -> ParityQuote | None: ...
    async def get_stablecoin_rate(self, pair: str) -> ParityQuote | None: ...
    async def get_crypto_kimchi_premium(self, symbol: str) -> dict[str, Any] | None: ...
    async def get_synthetic_quote(self, symbol: str) -> ParityQuote | None: ...
    async def get_kr_stock_quote(self, symbol: str) -> ParityQuote | None: ...


class DefaultMarketParityProvider:
    """Read-only provider backed only by currently owned/approved adapters.

    ETF proxy, FX, stablecoin, and Hyperliquid synthetic sources are returned as
    missing/disabled by default until the user approves source selection and
    collector activation. Tests can inject a provider with those legs to verify
    calculations without production dependencies.
    """

    async def get_index_quote(self, symbol: str) -> ParityQuote | None:
        payload = await handle_get_market_index_current_only(symbol)
        row = _first_index_row(payload, symbol)
        if row is None:
            return None
        price = _decimal_or_none(row.get("current") or row.get("price"))
        if price is None:
            return None
        data_state = str(row.get("data_state")) if row.get("data_state") else None
        return ParityQuote(
            symbol=symbol,
            price=price,
            source=str(row.get("source") or "market_index"),
            as_of=_parse_datetime(
                row.get("quote_asof") or row.get("as_of") or row.get("timestamp")
            ),
            stale=data_state == "stale",
            warnings=tuple([str(row["error"])] if row.get("error") else []),
        )

    async def get_proxy_quote(self, symbol: str) -> ParityQuote | None:
        _ = symbol
        return None

    async def get_fx_rate(self, pair: str) -> ParityQuote | None:
        _ = pair
        return None

    async def get_stablecoin_rate(self, pair: str) -> ParityQuote | None:
        _ = pair
        return None

    async def get_crypto_kimchi_premium(self, symbol: str) -> dict[str, Any] | None:
        return await handle_get_kimchi_premium(symbol)

    async def get_synthetic_quote(self, symbol: str) -> ParityQuote | None:
        _ = symbol
        return None

    async def get_kr_stock_quote(self, symbol: str) -> ParityQuote | None:
        _ = symbol
        return None


def _now() -> datetime:
    return datetime.now(UTC)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _first_index_row(payload: Any, symbol: str) -> dict[str, Any] | None:
    rows: list[Any]
    if isinstance(payload, dict):
        if isinstance(payload.get("indices"), list):
            rows = payload["indices"]
        else:
            rows = [payload]
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    wanted = symbol.upper()
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_symbol = str(row.get("symbol") or row.get("code") or "").upper()
        if row_symbol == wanted or len(rows) == 1:
            return row
    return None


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator == 0:
        return None
    return ((numerator / denominator) - Decimal("1")) * Decimal("100")


def _round_decimal(value: Decimal | None, places: str = "0.01") -> float | None:
    if value is None:
        return None
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _tone(premium_pct: float | None) -> InvestParityTone:
    if premium_pct is None:
        return "unknown"
    if premium_pct > 0:
        return "premium"
    if premium_pct < 0:
        return "discount"
    return "flat"


def _source(
    *,
    source: str,
    source_of_truth: str,
    legs: list[ParityQuote | None] | None = None,
    warnings: list[str] | None = None,
) -> InvestParitySource:
    real_legs = [leg for leg in (legs or []) if leg is not None]
    as_of_values = [leg.as_of for leg in real_legs if leg.as_of is not None]
    return InvestParitySource(
        source=source,
        sourceOfTruth=source_of_truth,
        asOf=max(as_of_values) if as_of_values else None,
        stale=any(leg.stale for leg in real_legs),
        warnings=[*(warnings or []), *(w for leg in real_legs for w in leg.warnings)],
    )


async def _capture(label: str, call: Any) -> tuple[Any | None, str | None]:
    try:
        return await asyncio.wait_for(call(), timeout=6), None
    except Exception as exc:  # provider failures degrade the /invest shell
        return None, f"{label}: {exc}"


def _missing_index_card(
    config: IndexParityConfig, missing_reasons: list[str], warnings: list[str]
) -> InvestMarketParityCard:
    reason = missing_reasons[0] if missing_reasons else "index_parity_source_missing"
    return InvestMarketParityCard(
        id=config.id,
        type="index_implied_parity",
        title=config.title,
        baseSymbol=config.base_symbol,
        proxySymbol=config.proxy_symbol,
        formula=_INDEX_PARITY_FORMULA,
        dataState="missing",
        emptyReason=reason,
        source=_source(
            source="not_configured",
            source_of_truth="market_index/proxy_quote/fx",
            warnings=[*missing_reasons, *warnings],
        ),
    )


async def _build_index_card(
    provider: MarketParityProvider, config: IndexParityConfig
) -> tuple[InvestMarketParityCard, list[str]]:
    base, base_warning = await _capture(
        f"index:{config.base_symbol}",
        lambda: provider.get_index_quote(config.base_symbol),
    )
    proxy, proxy_warning = await _capture(
        f"proxy:{config.proxy_symbol}",
        lambda: provider.get_proxy_quote(config.proxy_symbol),
    )
    fx, fx_warning = await _capture(
        "fx:USD/KRW", lambda: provider.get_fx_rate("USD/KRW")
    )
    warnings = [w for w in [base_warning, proxy_warning, fx_warning] if w]
    missing: list[str] = []
    if base is None:
        missing.append("market_index_unavailable")
    if proxy is None:
        missing.append("proxy_quote_missing")
    if fx is None:
        missing.append("fx_source_not_configured")
    if missing:
        return _missing_index_card(config, missing, warnings), warnings

    implied = proxy.price * fx.price * config.divisor
    premium = _round_decimal(_pct(implied, base.price))
    return (
        InvestMarketParityCard(
            id=config.id,
            type="index_implied_parity",
            title=config.title,
            baseSymbol=config.base_symbol,
            proxySymbol=config.proxy_symbol,
            basePrice=_round_decimal(base.price, "0.0001"),
            proxyPrice=_round_decimal(proxy.price, "0.0001"),
            fxRate=_round_decimal(fx.price, "0.0001"),
            impliedValue=_round_decimal(implied, "0.0001"),
            premiumPct=premium,
            tone=_tone(premium),
            formula=_INDEX_PARITY_FORMULA,
            dataState="stale"
            if any(leg.stale for leg in [base, proxy, fx])
            else "fresh",
            source=_source(
                source="market_index+proxy+fx",
                source_of_truth="market_index/provider_fixture/fx",
                legs=[base, proxy, fx],
                warnings=warnings,
            ),
        ),
        warnings,
    )


async def _build_stablecoin_card(
    provider: MarketParityProvider,
) -> tuple[InvestMarketParityCard, list[str]]:
    usd, usd_warning = await _capture(
        "fx:USD/KRW", lambda: provider.get_fx_rate("USD/KRW")
    )
    usdt, usdt_warning = await _capture(
        "stablecoin:USDT/KRW", lambda: provider.get_stablecoin_rate("USDT/KRW")
    )
    warnings = [w for w in [usd_warning, usdt_warning] if w]
    missing: list[str] = []
    if usd is None:
        missing.append("fx_source_not_configured")
    if usdt is None:
        missing.append("stablecoin_fx_source_not_configured")
    if missing:
        return (
            InvestMarketParityCard(
                id="usdt-krw-usd-krw-premium",
                type="stablecoin_fx_premium",
                title="USDT/KRW vs USD/KRW premium",
                usdKrw=_round_decimal(usd.price, "0.0001") if usd else None,
                usdtKrw=_round_decimal(usdt.price, "0.0001") if usdt else None,
                formula=_STABLECOIN_FX_FORMULA,
                dataState="missing",
                emptyReason=missing[0],
                source=_source(
                    source="not_configured",
                    source_of_truth="approval_gate",
                    legs=[usd, usdt],
                    warnings=[*missing, *warnings],
                ),
            ),
            warnings,
        )
    premium = _round_decimal(_pct(usdt.price, usd.price))
    return (
        InvestMarketParityCard(
            id="usdt-krw-usd-krw-premium",
            type="stablecoin_fx_premium",
            title="USDT/KRW vs USD/KRW premium",
            usdKrw=_round_decimal(usd.price, "0.0001"),
            usdtKrw=_round_decimal(usdt.price, "0.0001"),
            premiumPct=premium,
            tone=_tone(premium),
            formula=_STABLECOIN_FX_FORMULA,
            dataState="stale" if usd.stale or usdt.stale else "fresh",
            source=_source(
                source="fx+stablecoin",
                source_of_truth="provider_fixture/fx",
                legs=[usd, usdt],
                warnings=warnings,
            ),
        ),
        warnings,
    )


def _extract_kimchi(
    payload: Any,
) -> tuple[float | None, str, datetime | None, list[str]]:
    row: dict[str, Any] | None = None
    if isinstance(payload, list) and payload:
        first = payload[0]
        row = first if isinstance(first, dict) else None
    elif isinstance(payload, dict):
        row = payload
    if row is None:
        return None, "BTC", None, ["crypto_kimchi_payload_unavailable"]
    premium = _decimal_or_none(
        row.get("premium_pct") or row.get("kimchi_premium") or row.get("premium")
    )
    symbol = str(row.get("symbol") or row.get("market") or "BTC")
    warnings = [str(row["error"])] if row.get("error") else []
    return _round_decimal(premium), symbol, _parse_datetime(row.get("as_of")), warnings


async def _build_kimchi_card(
    provider: MarketParityProvider,
) -> tuple[InvestMarketParityCard, list[str]]:
    payload, warning = await _capture(
        "crypto_kimchi:BTC", lambda: provider.get_crypto_kimchi_premium("BTC")
    )
    premium, symbol, as_of, payload_warnings = _extract_kimchi(payload)
    warnings = [w for w in [warning, *payload_warnings] if w]
    state: InvestParityState = (
        "fresh" if premium is not None and not warnings else "missing"
    )
    return (
        InvestMarketParityCard(
            id="btc-kimchi-premium",
            type="crypto_kimchi_premium",
            title="BTC kimchi premium",
            baseSymbol=symbol,
            premiumPct=premium,
            tone=_tone(premium),
            dataState=state,
            emptyReason=None if premium is not None else "crypto_kimchi_unavailable",
            source=InvestParitySource(
                source="upbit+binance" if premium is not None else "not_available",
                sourceOfTruth="get_kimchi_premium(BTC)",
                asOf=as_of,
                stale=False,
                warnings=warnings,
            ),
        ),
        warnings,
    )


async def _build_synthetic_card(
    provider: MarketParityProvider, config: SyntheticParityConfig
) -> tuple[InvestMarketParityCard, list[str]]:
    base, base_warning = await _capture(
        f"kr_stock:{config.base_symbol}",
        lambda: provider.get_kr_stock_quote(config.base_symbol),
    )
    synthetic, synthetic_warning = await _capture(
        f"synthetic:{config.synthetic_symbol}",
        lambda: provider.get_synthetic_quote(config.synthetic_symbol),
    )
    fx, fx_warning = await _capture(
        "fx:USD/KRW", lambda: provider.get_fx_rate("USD/KRW")
    )
    warnings = [w for w in [base_warning, synthetic_warning, fx_warning] if w]
    if synthetic is None:
        return (
            InvestMarketParityCard(
                id=f"{config.base_symbol}-{config.synthetic_symbol.lower().replace(':', '-')}",
                type="synthetic_kr_stock_parity",
                title=config.title,
                baseSymbol=config.base_symbol,
                baseName=config.base_name,
                syntheticSymbol=config.synthetic_symbol,
                basePrice=_round_decimal(base.price, "0.0001") if base else None,
                fxRate=_round_decimal(fx.price, "0.0001") if fx else None,
                formula=_SYNTHETIC_FORMULA,
                dataState="disabled",
                emptyReason="hyperliquid_source_not_approved",
                source=_source(
                    source="not_configured",
                    source_of_truth="approval_gate",
                    legs=[base, fx],
                    warnings=["hyperliquid_source_not_approved", *warnings],
                ),
            ),
            warnings,
        )
    missing = []
    if base is None:
        missing.append("kr_stock_quote_missing")
    if fx is None:
        missing.append("fx_source_not_configured")
    if missing:
        return (
            InvestMarketParityCard(
                id=f"{config.base_symbol}-{config.synthetic_symbol.lower().replace(':', '-')}",
                type="synthetic_kr_stock_parity",
                title=config.title,
                baseSymbol=config.base_symbol,
                baseName=config.base_name,
                syntheticSymbol=config.synthetic_symbol,
                syntheticPrice=_round_decimal(synthetic.price, "0.0001"),
                formula=_SYNTHETIC_FORMULA,
                dataState="missing",
                emptyReason=missing[0],
                source=_source(
                    source="hyperliquid_fixture",
                    source_of_truth="provider_fixture/approval_gate",
                    legs=[base, synthetic, fx],
                    warnings=[*missing, *warnings],
                ),
            ),
            warnings,
        )
    implied = synthetic.price * fx.price
    premium = _round_decimal(_pct(implied, base.price))
    return (
        InvestMarketParityCard(
            id=f"{config.base_symbol}-{config.synthetic_symbol.lower().replace(':', '-')}",
            type="synthetic_kr_stock_parity",
            title=config.title,
            baseSymbol=config.base_symbol,
            baseName=config.base_name,
            syntheticSymbol=config.synthetic_symbol,
            basePrice=_round_decimal(base.price, "0.0001"),
            syntheticPrice=_round_decimal(synthetic.price, "0.0001"),
            fxRate=_round_decimal(fx.price, "0.0001"),
            impliedValue=_round_decimal(implied, "0.0001"),
            premiumPct=premium,
            tone=_tone(premium),
            formula=_SYNTHETIC_FORMULA,
            dataState="stale"
            if any(leg.stale for leg in [base, synthetic, fx])
            else "fresh",
            source=_source(
                source="kr_quote+synthetic+fx",
                source_of_truth="provider_fixture/hyperliquid_approval_required",
                legs=[base, synthetic, fx],
                warnings=warnings,
            ),
        ),
        warnings,
    )


def _response_state(
    cards: list[InvestMarketParityCard], warnings: list[str]
) -> InvestParityState:
    if not cards:
        return "missing"
    usable = [card for card in cards if card.dataState in {"fresh", "stale", "partial"}]
    if not usable:
        return "missing"
    if (
        warnings
        or len(usable) < len(cards)
        or any(card.dataState != "fresh" for card in usable)
    ):
        return "partial"
    return "fresh"


async def build_market_parity(
    provider: MarketParityProvider | None = None,
    *,
    market: str = "kr",
    include_disabled: bool = True,
    limit: int = 20,
) -> InvestMarketParityResponse:
    if market != "kr":
        raise ValueError("market_parity_only_supports_kr")
    provider = provider or DefaultMarketParityProvider()
    as_of = _now()
    warnings: list[str] = []
    cards: list[InvestMarketParityCard] = []

    index_config = IndexParityConfig(
        id="ewy-kospi-implied-parity",
        title="EWY implied KOSPI parity",
        base_symbol="KOSPI",
        proxy_symbol="EWY",
    )
    synthetic_configs = [
        SyntheticParityConfig(
            base_symbol="005930",
            base_name="삼성전자",
            synthetic_symbol="xyz:SMSN",
            title="Samsung Electronics synthetic parity",
        ),
        SyntheticParityConfig(
            base_symbol="000660",
            base_name="SK하이닉스",
            synthetic_symbol="xyz:SKHX",
            title="SK hynix synthetic parity",
        ),
    ][: max(limit, 0)]

    # ROB-689: the four card builders are independent; gather them so the two
    # network-bound cards (index=KOSPI/naver, kimchi=upbit+binance+er-api) overlap
    # instead of summing. Order of cards + warnings is preserved exactly (gather
    # returns results positionally), so the response is byte-identical to serial.
    (
        (index_card, index_warnings),
        (stablecoin_card, stablecoin_warnings),
        (kimchi_card, kimchi_warnings),
        *synthetic_results,
    ) = await asyncio.gather(
        _build_index_card(provider, index_config),
        _build_stablecoin_card(provider),
        _build_kimchi_card(provider),
        *(_build_synthetic_card(provider, config) for config in synthetic_configs),
    )

    cards.append(index_card)
    warnings.extend(index_warnings)
    cards.append(stablecoin_card)
    warnings.extend(stablecoin_warnings)
    cards.append(kimchi_card)
    warnings.extend(kimchi_warnings)
    for card, card_warnings in synthetic_results:
        cards.append(card)
        warnings.extend(card_warnings)

    if not include_disabled:
        cards = [card for card in cards if card.dataState != "disabled"]

    # Card-level source warnings carry expected approval-gate diagnostics. Top-level
    # warnings are reserved for provider failures/unexpected partial failures.
    unique_warnings = list(dict.fromkeys(warnings))
    state = _response_state(cards, unique_warnings)
    return InvestMarketParityResponse(
        market="kr",
        state=state,
        asOf=as_of,
        cards=cards,
        emptyReason="no_market_parity_cards" if not cards else None,
        warnings=unique_warnings,
        notes=[
            "Read-only market parity dashboard; no broker/order/watch mutations.",
            "raoni.xyz is benchmark-only and is not queried in production.",
            "Hyperliquid, ETF proxy, FX, and stablecoin collectors remain approval-gated unless explicitly supplied by an owned provider/fixture.",
        ],
    )

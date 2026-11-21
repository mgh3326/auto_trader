from __future__ import annotations

import datetime as dt
import os
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

ALTERNATIVE_ME_FNG_URL = "https://api.alternative.me/fng/"
COINGECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"
BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
DEFILLAMA_CHAINS_URL = "https://api.llama.fi/v2/chains"
DEFILLAMA_STABLECOINS_URL = "https://stablecoins.llama.fi/stablecoins"
COINGLASS_OPEN_INTEREST_URL = (
    "https://open-api-v4.coinglass.com/api/futures/openInterest/ohlc-history"
)
TOKENOMIST_UNLOCKS_URL = "https://api.tokenomist.ai/v1/unlocks"
TRADINGVIEW_REFERENCE_URL = (
    "https://www.tradingview.com/markets/cryptocurrencies/prices-all/"
)


@dataclass(frozen=True)
class CryptoInsightMetric:
    metric: str
    provider: str
    symbol: str | None
    value: Decimal | None
    unit: str | None
    label: str | None
    source_url: str
    observed_at: dt.datetime
    freshness_seconds: int | None
    raw_payload: dict[str, Any]


@dataclass(frozen=True)
class CryptoInsightProviderResult:
    metrics: tuple[CryptoInsightMetric, ...] = ()
    warnings: tuple[str, ...] = ()


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def _freshness_from_unix(
    value: Any,
    now: dt.datetime,
    time_until_update: Any = None,
) -> int | None:
    try:
        timestamp = int(value)
    except Exception:  # noqa: BLE001
        return None
    observed = dt.datetime.fromtimestamp(timestamp, tz=dt.UTC)
    age_seconds = max(0, int((now - observed).total_seconds()))
    if time_until_update is not None:
        try:
            return age_seconds + max(0, int(time_until_update))
        except Exception:  # noqa: BLE001
            pass
    # Alternative.me is daily; when the live response lacks time_until_update,
    # keep one day of freshness from the value timestamp rather than expiring at
    # fetch time. The caller still compares this against snapshot_at age.
    return 24 * 3600


async def fetch_alternative_me_fear_greed(
    client: httpx.AsyncClient | None = None,
    *,
    now: dt.datetime | None = None,
) -> CryptoInsightProviderResult:
    observed_at = (now or _utc_now()).astimezone(dt.UTC).replace(microsecond=0)
    close_client = client is None
    http = client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await http.get(ALTERNATIVE_ME_FNG_URL, params={"limit": 2})
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data") or []
        if not rows:
            return CryptoInsightProviderResult(
                warnings=("alternative_me: empty fear/greed payload",)
            )
        current = rows[0]
        value = _decimal(current.get("value"))
        timestamp = current.get("timestamp")
        freshness = _freshness_from_unix(
            timestamp, observed_at, current.get("time_until_update")
        )
        if timestamp is not None:
            try:
                observed_at = dt.datetime.fromtimestamp(int(timestamp), tz=dt.UTC)
            except Exception:  # noqa: BLE001
                pass
        return CryptoInsightProviderResult(
            metrics=(
                CryptoInsightMetric(
                    metric="fear_greed",
                    provider="alternative_me",
                    symbol=None,
                    value=value,
                    unit="score",
                    label=current.get("value_classification"),
                    source_url=ALTERNATIVE_ME_FNG_URL,
                    observed_at=observed_at,
                    freshness_seconds=freshness,
                    raw_payload={
                        "current": current,
                        "previous": rows[1] if len(rows) > 1 else None,
                    },
                ),
            )
        )
    except Exception as exc:  # noqa: BLE001
        return CryptoInsightProviderResult(warnings=(f"alternative_me: {exc}",))
    finally:
        if close_client:
            await http.aclose()


async def fetch_coingecko_global(
    client: httpx.AsyncClient | None = None,
    *,
    now: dt.datetime | None = None,
) -> CryptoInsightProviderResult:
    observed_at = (now or _utc_now()).astimezone(dt.UTC).replace(microsecond=0)
    close_client = client is None
    http = client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await http.get(COINGECKO_GLOBAL_URL)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or {}
        cap_pct = data.get("market_cap_percentage") or {}
        metrics = [
            CryptoInsightMetric(
                metric="btc_dominance",
                provider="coingecko",
                symbol="BTC",
                value=_decimal(cap_pct.get("btc")),
                unit="%",
                label=None,
                source_url=COINGECKO_GLOBAL_URL,
                observed_at=observed_at,
                freshness_seconds=0,
                raw_payload={"market_cap_percentage": cap_pct},
            ),
            CryptoInsightMetric(
                metric="global_market_cap_change_24h",
                provider="coingecko",
                symbol=None,
                value=_decimal(data.get("market_cap_change_percentage_24h_usd")),
                unit="%",
                label=None,
                source_url=COINGECKO_GLOBAL_URL,
                observed_at=observed_at,
                freshness_seconds=0,
                raw_payload={
                    "market_cap_change_percentage_24h_usd": data.get(
                        "market_cap_change_percentage_24h_usd"
                    )
                },
            ),
        ]
        return CryptoInsightProviderResult(
            metrics=tuple(m for m in metrics if m.value is not None)
        )
    except Exception as exc:  # noqa: BLE001
        return CryptoInsightProviderResult(warnings=(f"coingecko: {exc}",))
    finally:
        if close_client:
            await http.aclose()


async def fetch_binance_funding_rates(
    symbols: Sequence[str] = ("BTCUSDT", "ETHUSDT"),
    client: httpx.AsyncClient | None = None,
    *,
    now: dt.datetime | None = None,
) -> CryptoInsightProviderResult:
    observed_at = (now or _utc_now()).astimezone(dt.UTC).replace(microsecond=0)
    close_client = client is None
    http = client or httpx.AsyncClient(timeout=10.0)
    metrics: list[CryptoInsightMetric] = []
    warnings: list[str] = []
    try:
        for symbol in [s.strip().upper() for s in symbols if s.strip()]:
            try:
                response = await http.get(
                    BINANCE_FUNDING_URL, params={"symbol": symbol}
                )
                response.raise_for_status()
                payload = response.json()
                rate = _decimal(payload.get("lastFundingRate"))
                label = None
                if rate is not None:
                    label = (
                        "longs pay shorts"
                        if rate > 0
                        else "shorts pay longs"
                        if rate < 0
                        else "neutral"
                    )
                metrics.append(
                    CryptoInsightMetric(
                        metric="funding_rate",
                        provider="binance",
                        symbol=symbol,
                        value=rate,
                        unit="ratio",
                        label=label,
                        source_url=BINANCE_FUNDING_URL,
                        observed_at=observed_at,
                        freshness_seconds=_freshness_from_unix(
                            (payload.get("time") or 0) // 1000
                            if isinstance(payload.get("time"), int)
                            else None,
                            observed_at,
                        ),
                        raw_payload=payload,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"binance:{symbol}: {exc}")
    finally:
        if close_client:
            await http.aclose()
    return CryptoInsightProviderResult(metrics=tuple(metrics), warnings=tuple(warnings))


UPBIT_TICKER_URL = "https://api.upbit.com/v1/ticker"
BINANCE_SPOT_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"
OPEN_ER_API_USD_URL = "https://open.er-api.com/v6/latest/USD"
BINANCE_FUNDING_HISTORY_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
KIMCHI_PREMIUM_METRIC = "kimchi_premium"
KIMCHI_PREMIUM_PROVIDER = "upbit_binance"
FUNDING_SETTLEMENT_METRIC = "funding_settlement_rate"
FUNDING_SETTLEMENT_PROVIDER = "binance_settlement"
FUNDING_PRINT_COUNT = 3
_KIMCHI_QUANTUM = Decimal("0.0001")


def _finite_decimal(value: Any) -> Decimal | None:
    """Parse a provider number exactly; NaN/Inf/bool/unparseable are None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None
    return parsed if parsed.is_finite() else None


def _positive_decimal(value: Any) -> Decimal | None:
    parsed = _finite_decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


async def _upbit_btc_krw_price(http: httpx.AsyncClient) -> tuple[Decimal, Any]:
    response = await http.get(UPBIT_TICKER_URL, params={"markets": "KRW-BTC"})
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("unexpected upbit ticker payload")
    row = rows[0]
    if row.get("market") != "KRW-BTC":
        raise ValueError("upbit ticker market mismatch")
    price = _positive_decimal(row.get("trade_price"))
    if price is None:
        raise ValueError("upbit trade_price unreadable")
    return price, row.get("trade_timestamp")


async def _binance_btc_usdt_price(http: httpx.AsyncClient) -> Decimal:
    response = await http.get(BINANCE_SPOT_PRICE_URL, params={"symbol": "BTCUSDT"})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("symbol") != "BTCUSDT":
        raise ValueError("unexpected binance spot payload")
    price = _positive_decimal(payload.get("price"))
    if price is None:
        raise ValueError("binance spot price unreadable")
    return price


async def _open_er_usd_krw_rate(http: httpx.AsyncClient) -> Decimal:
    response = await http.get(OPEN_ER_API_USD_URL)
    response.raise_for_status()
    payload = response.json()
    rates = payload.get("rates") if isinstance(payload, dict) else None
    rate = _positive_decimal(rates.get("KRW") if isinstance(rates, dict) else None)
    if rate is None:
        raise ValueError("usd/krw rate unreadable")
    return rate


async def fetch_upbit_binance_kimchi_premium(
    client: httpx.AsyncClient | None = None,
    *,
    now: dt.datetime | None = None,
) -> CryptoInsightProviderResult:
    """BTC kimchi premium: Upbit KRW vs Binance USDT converted at open.er-api USD/KRW.

    Fail-closed: all three legs must be readable. A missing or unreadable leg yields
    no metric and a warning, so the regime gate sees unknown, never zero.
    """
    observed_at = (now or _utc_now()).astimezone(dt.UTC).replace(microsecond=0)
    close_client = client is None
    http = client or httpx.AsyncClient(timeout=10.0)
    try:
        upbit_krw, upbit_trade_ts = await _upbit_btc_krw_price(http)
        binance_usdt = await _binance_btc_usdt_price(http)
        usd_krw = await _open_er_usd_krw_rate(http)
    except Exception as exc:  # noqa: BLE001
        return CryptoInsightProviderResult(
            warnings=(f"{KIMCHI_PREMIUM_PROVIDER}: {exc}",)
        )
    finally:
        if close_client:
            await http.aclose()
    global_krw = binance_usdt * usd_krw
    premium_pct = ((upbit_krw - global_krw) / global_krw * Decimal(100)).quantize(
        _KIMCHI_QUANTUM
    )
    return CryptoInsightProviderResult(
        metrics=(
            CryptoInsightMetric(
                metric=KIMCHI_PREMIUM_METRIC,
                provider=KIMCHI_PREMIUM_PROVIDER,
                symbol="BTC",
                value=premium_pct,
                unit="%",
                label=None,
                source_url=UPBIT_TICKER_URL,
                observed_at=observed_at,
                freshness_seconds=0,
                raw_payload={
                    "upbit_krw": str(upbit_krw),
                    "upbit_trade_timestamp_ms": upbit_trade_ts,
                    "binance_usdt": str(binance_usdt),
                    "usd_krw": str(usd_krw),
                    "global_krw": str(global_krw),
                    "observed_at": observed_at.isoformat(),
                    "sources": [
                        UPBIT_TICKER_URL,
                        BINANCE_SPOT_PRICE_URL,
                        OPEN_ER_API_USD_URL,
                    ],
                },
            ),
        )
    )


def _funding_label(rate: Decimal) -> str:
    if rate > 0:
        return "longs pay shorts"
    if rate < 0:
        return "shorts pay longs"
    return "neutral"


async def fetch_binance_settlement_funding_prints(
    client: httpx.AsyncClient | None = None,
    *,
    symbol: str = "BTCUSDT",
    now: dt.datetime | None = None,
) -> CryptoInsightProviderResult:
    """The last FUNDING_PRINT_COUNT settlement prints, one row per settlement.

    snapshot_at is the settlement time, so re-running the job upserts the same
    rows. All-or-nothing: fewer, more, duplicate or malformed prints yield no
    metric and a warning, because a 3-print average over 2 prints is a wrong value.
    """
    observed_at = (now or _utc_now()).astimezone(dt.UTC).replace(microsecond=0)
    close_client = client is None
    http = client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await http.get(
            BINANCE_FUNDING_HISTORY_URL,
            params={"symbol": symbol, "limit": FUNDING_PRINT_COUNT},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("unexpected funding history payload")
        prints: list[tuple[int, Decimal, dict[str, Any]]] = []
        for row in payload:
            if not isinstance(row, dict) or str(row.get("symbol") or "") != symbol:
                raise ValueError("funding row symbol mismatch")
            settle_ms = row.get("fundingTime")
            if (
                not isinstance(settle_ms, int)
                or isinstance(settle_ms, bool)
                or settle_ms <= 0
            ):
                raise ValueError("funding fundingTime unreadable")
            rate = _finite_decimal(row.get("fundingRate"))
            if rate is None:
                raise ValueError("funding fundingRate unreadable")
            prints.append((settle_ms, rate, row))
        if len(prints) != FUNDING_PRINT_COUNT:
            raise ValueError(
                f"expected {FUNDING_PRINT_COUNT} funding prints, got {len(prints)}"
            )
        if len({settle_ms for settle_ms, _, _ in prints}) != FUNDING_PRINT_COUNT:
            raise ValueError("duplicate funding settlement times")
    except Exception as exc:  # noqa: BLE001
        return CryptoInsightProviderResult(
            warnings=(f"{FUNDING_SETTLEMENT_PROVIDER}:{symbol}: {exc}",)
        )
    finally:
        if close_client:
            await http.aclose()
    metrics: list[CryptoInsightMetric] = []
    for settle_ms, rate, row in sorted(prints, key=lambda item: item[0]):
        settled_at = dt.datetime.fromtimestamp(settle_ms / 1000, tz=dt.UTC)
        metrics.append(
            CryptoInsightMetric(
                metric=FUNDING_SETTLEMENT_METRIC,
                provider=FUNDING_SETTLEMENT_PROVIDER,
                symbol=symbol,
                value=rate,
                unit="ratio",
                label=_funding_label(rate),
                source_url=BINANCE_FUNDING_HISTORY_URL,
                observed_at=settled_at,
                freshness_seconds=max(
                    0, int((observed_at - settled_at).total_seconds())
                ),
                raw_payload={
                    "symbol": symbol,
                    "fundingTime": settle_ms,
                    "fundingRate": str(rate),
                    "markPrice": row.get("markPrice"),
                    "rateType": row.get("rateType"),
                    "observed_at": observed_at.isoformat(),
                },
            )
        )
    return CryptoInsightProviderResult(metrics=tuple(metrics))


def _matches_defillama_slug(row: dict[str, Any], slug: str) -> bool:
    candidates = (
        row.get("gecko_id"),
        row.get("name"),
        row.get("tokenSymbol"),
        row.get("symbol"),
    )
    return any(str(value).strip().lower() == slug for value in candidates if value)


def _stablecoin_usd_total(payload: dict[str, Any]) -> Decimal | None:
    direct = _decimal(payload.get("totalCirculatingUSD")) or _decimal(
        payload.get("totalCirculating")
    )
    if direct is not None:
        return direct
    total = Decimal("0")
    found = False
    for chain in payload.get("chains") or []:
        if not isinstance(chain, dict):
            continue
        circulating = chain.get("totalCirculatingUSD")
        value = (
            circulating.get("peggedUSD")
            if isinstance(circulating, dict)
            else circulating
        )
        amount = _decimal(value)
        if amount is not None:
            total += amount
            found = True
    return total if found else None


async def fetch_defillama_reference(
    client: httpx.AsyncClient | None = None,
    *,
    protocol_slugs: Sequence[str] = ("bitcoin", "ethereum"),
    now: dt.datetime | None = None,
) -> CryptoInsightProviderResult:
    observed_at = (now or _utc_now()).astimezone(dt.UTC).replace(microsecond=0)
    close_client = client is None
    http = client or httpx.AsyncClient(timeout=10.0)
    metrics: list[CryptoInsightMetric] = []
    warnings: list[str] = []
    try:
        wanted_slugs = [s.strip().lower() for s in protocol_slugs if s.strip()]
        try:
            response = await http.get(DEFILLAMA_CHAINS_URL)
            response.raise_for_status()
            payload = response.json()
            chain_rows = (
                [row for row in payload if isinstance(row, dict)]
                if isinstance(payload, list)
                else []
            )
            for slug in wanted_slugs:
                row = next(
                    (r for r in chain_rows if _matches_defillama_slug(r, slug)), None
                )
                if row is None:
                    warnings.append(f"defillama:{slug}: chain not found")
                    continue
                metrics.append(
                    CryptoInsightMetric(
                        metric="tvl",
                        provider="defillama",
                        symbol=(
                            row.get("tokenSymbol") or row.get("symbol") or slug
                        ).upper(),
                        value=_decimal(row.get("tvl")),
                        unit="usd",
                        label=row.get("name"),
                        source_url=DEFILLAMA_CHAINS_URL,
                        observed_at=observed_at,
                        freshness_seconds=0,
                        raw_payload={
                            "name": row.get("name"),
                            "tokenSymbol": row.get("tokenSymbol"),
                            "gecko_id": row.get("gecko_id"),
                            "tvl": row.get("tvl"),
                        },
                    )
                )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"defillama:chains: {exc}")
        try:
            response = await http.get(
                DEFILLAMA_STABLECOINS_URL, params={"includePrices": "false"}
            )
            response.raise_for_status()
            payload = response.json()
            total = _stablecoin_usd_total(payload if isinstance(payload, dict) else {})
            metrics.append(
                CryptoInsightMetric(
                    metric="stablecoin_supply",
                    provider="defillama",
                    symbol=None,
                    value=total,
                    unit="usd",
                    label="stablecoins circulating",
                    source_url=DEFILLAMA_STABLECOINS_URL,
                    observed_at=observed_at,
                    freshness_seconds=0,
                    raw_payload={
                        "totalCirculatingUSD": str(total) if total is not None else None
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"defillama:stablecoins: {exc}")
    finally:
        if close_client:
            await http.aclose()
    return CryptoInsightProviderResult(
        metrics=tuple(m for m in metrics if m.value is not None),
        warnings=tuple(warnings),
    )


async def fetch_coinglass_open_interest_poc(
    *,
    api_key: str | None = None,
) -> CryptoInsightProviderResult:
    if not api_key:
        return CryptoInsightProviderResult(
            warnings=("coinglass: disabled (missing API key)",)
        )
    return CryptoInsightProviderResult(
        warnings=("coinglass: PoC adapter defined but not enabled by default",)
    )


async def fetch_tokenomist_unlocks_poc(
    *,
    api_key: str | None = None,
) -> CryptoInsightProviderResult:
    if not api_key:
        return CryptoInsightProviderResult(
            warnings=("tokenomist: disabled (missing API key)",)
        )
    return CryptoInsightProviderResult(
        warnings=("tokenomist: PoC adapter defined but not enabled by default",)
    )


async def fetch_tradingview_crypto_breadth_reference() -> CryptoInsightProviderResult:
    return CryptoInsightProviderResult(
        metrics=(
            CryptoInsightMetric(
                metric="tv_crypto_breadth",
                provider="tradingview",
                symbol=None,
                value=None,
                unit="count",
                label="reference-only: existing tvscreener path preserved",
                source_url=TRADINGVIEW_REFERENCE_URL,
                observed_at=_utc_now(),
                freshness_seconds=None,
                raw_payload={"status": "reference_only", "replace_tvscreener": False},
            ),
        ),
        warnings=("tradingview: reference adapter only; not a tvscreener replacement",),
    )


def coinglass_api_key_from_env() -> str | None:
    return os.getenv("COINGLASS_API_KEY") or os.getenv("COINGLASS_API_SECRET")


def tokenomist_api_key_from_env() -> str | None:
    return os.getenv("TOKENOMIST_API_KEY")

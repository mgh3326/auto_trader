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

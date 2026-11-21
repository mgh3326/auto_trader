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

# --- Gate-input timing contract (hk 5932, dots review B2) -------------------------
# Every gate-input row carries raw_payload["timing"] with three separate times:
#   event_at           economic time the value describes (funding: settlement time;
#                      kimchi: the latest input time, never later than receipt)
#   source_at          time the SOURCE says it produced the value, or None when the
#                      source gives none (never filled in from our own clock)
#   first_available_at when WE first received it; always the wall clock, never the
#                      injectable ``now``, so a backfill run cannot claim an earlier time
# A value is usable at a cutoff only if event_at AND first_available_at are <= cutoff.
# Rows with this key are insert-only in the repository (first write wins).
TIMING_KEY = "timing"
USDT_USD_ASSUMPTION = (
    "USDT is treated as exactly 1 USD: no USDT/USD cross rate is applied, so the "
    "premium also contains any USDT depeg"
)
# Upbit KRW-BTC trades continuously; a last trade older than this is a stale source.
KIMCHI_MAX_SPOT_SKEW_SECONDS = 120
# A source timestamp this far past our receive time is a clock fault, not a value.
SOURCE_FUTURE_TOLERANCE_SECONDS = 5
# open.er-api publishes one rate set per day. FX trading is closed from Friday
# 22:00 UTC to Sunday 22:00 UTC, so the rate legitimately carries Friday's value
# over the weekend and its age limit is longer then.
KIMCHI_FX_MAX_AGE_SECONDS = 36 * 3600
KIMCHI_FX_WEEKEND_MAX_AGE_SECONDS = 84 * 3600
# Perpetual funding periods Binance uses; a gap between prints must match one.
FUNDING_ALLOWED_PERIODS_SECONDS = (3600, 2 * 3600, 4 * 3600, 8 * 3600)
FUNDING_PERIOD_TOLERANCE_SECONDS = 60


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


def _epoch_to_utc(value: Any, *, unit: str) -> dt.datetime:
    """Strict int epoch (seconds or milliseconds) to an aware UTC datetime."""
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{unit} timestamp unreadable")
    return dt.datetime.fromtimestamp(value / (1000 if unit == "ms" else 1), tz=dt.UTC)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).replace(microsecond=0).isoformat()


def fx_market_closed(at: dt.datetime) -> bool:
    """FX trading is closed from Friday 22:00 UTC to Sunday 22:00 UTC."""
    at = at.astimezone(dt.UTC)
    weekday = at.weekday()  # Monday == 0
    return (
        (weekday == 4 and at.hour >= 22)
        or weekday == 5
        or (weekday == 6 and at.hour < 22)
    )


def funding_period_seconds(gap_seconds: float) -> int | None:
    for period in FUNDING_ALLOWED_PERIODS_SECONDS:
        if abs(gap_seconds - period) <= FUNDING_PERIOD_TOLERANCE_SECONDS:
            return period
    return None


async def _upbit_btc_krw_price(http: httpx.AsyncClient) -> tuple[Decimal, dt.datetime]:
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
    return price, _epoch_to_utc(row.get("trade_timestamp"), unit="ms")


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


async def _open_er_usd_krw_rate(http: httpx.AsyncClient) -> tuple[Decimal, dt.datetime]:
    response = await http.get(OPEN_ER_API_USD_URL)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("usd/krw rate unreadable")
    rates = payload.get("rates")
    rate = _positive_decimal(rates.get("KRW") if isinstance(rates, dict) else None)
    if rate is None:
        raise ValueError("usd/krw rate unreadable")
    return rate, _epoch_to_utc(payload.get("time_last_update_unix"), unit="s")


async def fetch_upbit_binance_kimchi_premium(
    client: httpx.AsyncClient | None = None,
    *,
    now: dt.datetime | None = None,
) -> CryptoInsightProviderResult:
    """BTC kimchi premium: Upbit KRW vs Binance USDT converted at open.er-api USD/KRW.

    Fail-closed: all three legs must be readable and their source times consistent
    (see the timing contract above). A missing, unreadable or stale leg yields no
    metric and a warning, so the regime gate sees unknown, never zero.

    ``now`` only feeds freshness; first_available_at is the real wall clock taken
    after the legs return, and snapshot_at is the event time, so a late run cannot
    place a value in the past.
    """
    close_client = client is None
    http = client or httpx.AsyncClient(timeout=10.0)
    try:
        upbit_krw, upbit_at = await _upbit_btc_krw_price(http)
        binance_usdt = await _binance_btc_usdt_price(http)
        usd_krw, fx_at = await _open_er_usd_krw_rate(http)
    except Exception as exc:  # noqa: BLE001
        return CryptoInsightProviderResult(
            warnings=(f"{KIMCHI_PREMIUM_PROVIDER}: {exc}",)
        )
    finally:
        if close_client:
            await http.aclose()
    received_at = _utc_now()
    # Binance's price endpoint carries no timestamp: the request time is the only
    # honest upper bound, so that leg's effective time is our receive time.
    effective = {"upbit_krw": upbit_at, "binance_usdt": received_at, "usd_krw": fx_at}
    future = dt.timedelta(seconds=SOURCE_FUTURE_TOLERANCE_SECONDS)
    for name, at in effective.items():
        if at > received_at + future:
            return CryptoInsightProviderResult(
                warnings=(
                    f"{KIMCHI_PREMIUM_PROVIDER}: {name} source time is in the future",
                )
            )
    spot_skew = abs((received_at - upbit_at).total_seconds())
    if spot_skew > KIMCHI_MAX_SPOT_SKEW_SECONDS:
        return CryptoInsightProviderResult(
            warnings=(
                f"{KIMCHI_PREMIUM_PROVIDER}: stale upbit trade "
                f"({int(spot_skew)}s old, limit {KIMCHI_MAX_SPOT_SKEW_SECONDS}s)",
            )
        )
    fx_age = max(0.0, (received_at - fx_at).total_seconds())
    fx_closed = fx_market_closed(received_at)
    fx_limit = (
        KIMCHI_FX_WEEKEND_MAX_AGE_SECONDS if fx_closed else KIMCHI_FX_MAX_AGE_SECONDS
    )
    if fx_age > fx_limit:
        return CryptoInsightProviderResult(
            warnings=(
                f"{KIMCHI_PREMIUM_PROVIDER}: stale usd/krw rate "
                f"({int(fx_age)}s old, limit {fx_limit}s)",
            )
        )
    event_at = max(effective.values())
    max_skew = (max(effective.values()) - min(effective.values())).total_seconds()
    global_krw = binance_usdt * usd_krw
    premium_pct = ((upbit_krw - global_krw) / global_krw * Decimal(100)).quantize(
        _KIMCHI_QUANTUM
    )
    freshness_base = (now or received_at).astimezone(dt.UTC)
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
                observed_at=event_at.replace(microsecond=0),
                freshness_seconds=max(
                    0, int((freshness_base - event_at).total_seconds())
                ),
                raw_payload={
                    "upbit_krw": str(upbit_krw),
                    "upbit_trade_timestamp_ms": int(upbit_at.timestamp() * 1000),
                    "binance_usdt": str(binance_usdt),
                    "usd_krw": str(usd_krw),
                    "global_krw": str(global_krw),
                    "observed_at": _iso(event_at),
                    "sources": [
                        UPBIT_TICKER_URL,
                        BINANCE_SPOT_PRICE_URL,
                        OPEN_ER_API_USD_URL,
                    ],
                    "inputs": {
                        "upbit_krw": {
                            "source_at": _iso(upbit_at),
                            "source_time_basis": "exchange_trade_timestamp",
                        },
                        "binance_usdt": {
                            "source_at": None,
                            "source_time_basis": "none_provided_request_time_used",
                        },
                        "usd_krw": {
                            "source_at": _iso(fx_at),
                            "source_time_basis": "provider_time_last_update_unix",
                            "source": OPEN_ER_API_USD_URL,
                            "market_closed_at_receipt": fx_closed,
                        },
                    },
                    TIMING_KEY: {
                        "event_at": _iso(event_at),
                        "source_at": _iso(max(upbit_at, fx_at)),
                        "first_available_at": _iso(received_at),
                        "max_skew_seconds": int(max_skew),
                        "spot_skew_seconds": int(spot_skew),
                        "fx_age_seconds": int(fx_age),
                        "fx_weekend_carry": fx_closed,
                        "usdt_usd_assumption": USDT_USD_ASSUMPTION,
                    },
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

    snapshot_at is the settlement time, so re-running the job hits the same rows
    (which the repository then keeps as first written). All-or-nothing: fewer, more,
    duplicate, malformed or non-consecutive prints yield no metric and a warning,
    because a 3-print average over 2 prints, or over a changed settlement period,
    is a wrong value. A rate of exactly 0 is a real print (value_state "zero");
    a missing rate is no row at all.

    first_available_at is the real wall clock at receipt (not ``now``), so prints
    fetched late are not usable at a cutoff before they were received.
    """
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
                raise ValueError("funding fundingRate missing or unreadable")
            prints.append((settle_ms, rate, row))
        if len(prints) != FUNDING_PRINT_COUNT:
            raise ValueError(
                f"expected {FUNDING_PRINT_COUNT} funding prints, got {len(prints)}"
            )
        if len({settle_ms for settle_ms, _, _ in prints}) != FUNDING_PRINT_COUNT:
            raise ValueError("duplicate funding settlement times")
        prints.sort(key=lambda item: item[0])
        periods = {
            funding_period_seconds((later[0] - earlier[0]) / 1000)
            for earlier, later in zip(prints, prints[1:], strict=False)
        }
        if len(periods) != 1 or None in periods:
            raise ValueError(
                "funding prints are not consecutive on one settlement period"
            )
        period_seconds = next(iter(periods))
    except Exception as exc:  # noqa: BLE001
        return CryptoInsightProviderResult(
            warnings=(f"{FUNDING_SETTLEMENT_PROVIDER}:{symbol}: {exc}",)
        )
    finally:
        if close_client:
            await http.aclose()
    received_at = _utc_now()
    freshness_base = (now or received_at).astimezone(dt.UTC)
    metrics: list[CryptoInsightMetric] = []
    for settle_ms, rate, row in prints:
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
                    0, int((freshness_base - settled_at).total_seconds())
                ),
                raw_payload={
                    "symbol": symbol,
                    "fundingTime": settle_ms,
                    "fundingRate": str(rate),
                    "markPrice": row.get("markPrice"),
                    "rateType": row.get("rateType"),
                    "observed_at": _iso(received_at),
                    "value_state": "zero" if rate == 0 else "nonzero",
                    "settlement_period_seconds": period_seconds,
                    TIMING_KEY: {
                        "event_at": _iso(settled_at),
                        # fundingRate history carries no publication time.
                        "source_at": None,
                        "first_available_at": _iso(received_at),
                    },
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

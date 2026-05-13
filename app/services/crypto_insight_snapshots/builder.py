from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.services.crypto_insight_snapshots.repository import (
    CryptoInsightSnapshotUpsert,
    redact_sensitive_payload,
)
from app.services.external.crypto_insights import (
    CryptoInsightMetric,
    CryptoInsightProviderResult,
    coinglass_api_key_from_env,
    fetch_alternative_me_fear_greed,
    fetch_binance_funding_rates,
    fetch_coingecko_global,
    fetch_coinglass_open_interest_poc,
    fetch_defillama_reference,
    fetch_tokenomist_unlocks_poc,
    fetch_tradingview_crypto_breadth_reference,
    tokenomist_api_key_from_env,
)

DEFAULT_PROVIDERS = ("alternative_me", "coingecko", "binance")
POC_PROVIDERS = ("defillama", "coinglass", "tokenomist", "tradingview")
ProviderFetcher = Callable[[], Awaitable[CryptoInsightProviderResult]]


@dataclass(frozen=True)
class CryptoInsightBuildResult:
    payloads: tuple[CryptoInsightSnapshotUpsert, ...]
    warnings: tuple[str, ...] = ()


def _metric_to_payload(metric: CryptoInsightMetric) -> CryptoInsightSnapshotUpsert:
    return CryptoInsightSnapshotUpsert(
        metric=metric.metric,
        provider=metric.provider,
        symbol=metric.symbol,
        value=metric.value,
        unit=metric.unit,
        label=metric.label,
        snapshot_at=metric.observed_at.astimezone(dt.UTC).replace(microsecond=0),
        source_url=metric.source_url,
        freshness_seconds=metric.freshness_seconds,
        raw_payload=redact_sensitive_payload(metric.raw_payload),
    )


def _provider_fetcher(
    provider: str,
    *,
    symbols: Sequence[str],
    now: dt.datetime | None,
) -> ProviderFetcher:
    provider_norm = provider.strip().lower()
    if provider_norm == "alternative_me":
        return lambda: fetch_alternative_me_fear_greed(now=now)
    if provider_norm == "coingecko":
        return lambda: fetch_coingecko_global(now=now)
    if provider_norm == "binance":
        funding_symbols = tuple(symbols or ("BTCUSDT", "ETHUSDT"))
        return lambda: fetch_binance_funding_rates(funding_symbols, now=now)
    if provider_norm == "defillama":
        return lambda: fetch_defillama_reference(now=now)
    if provider_norm == "coinglass":
        return lambda: fetch_coinglass_open_interest_poc(
            api_key=coinglass_api_key_from_env()
        )
    if provider_norm == "tokenomist":
        return lambda: fetch_tokenomist_unlocks_poc(
            api_key=tokenomist_api_key_from_env()
        )
    if provider_norm in {"tradingview", "tvscreener"}:
        return fetch_tradingview_crypto_breadth_reference
    raise ValueError(f"unsupported crypto insight provider: {provider}")


async def build_crypto_insight_snapshots(
    *,
    now: dt.datetime | None = None,
    providers: Sequence[str] | None = None,
    symbols: Sequence[str] | None = None,
    concurrency: int = 3,
    provider_fetchers: dict[str, ProviderFetcher] | None = None,
) -> CryptoInsightBuildResult:
    provider_names = tuple(
        p.strip().lower() for p in (providers or DEFAULT_PROVIDERS) if p.strip()
    )
    if not provider_names:
        return CryptoInsightBuildResult(
            payloads=(), warnings=("no providers requested",)
        )
    symbol_list = tuple(s.strip().upper() for s in (symbols or ()) if s.strip())
    fetchers: dict[str, ProviderFetcher] = {}
    warnings: list[str] = []
    for provider in provider_names:
        try:
            fetchers[provider] = (provider_fetchers or {}).get(
                provider
            ) or _provider_fetcher(provider, symbols=symbol_list, now=now)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{provider}: {exc}")
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _run(
        provider: str, fetcher: ProviderFetcher
    ) -> CryptoInsightProviderResult:
        async with sem:
            try:
                return await fetcher()
            except Exception as exc:  # noqa: BLE001
                return CryptoInsightProviderResult(warnings=(f"{provider}: {exc}",))

    results = await asyncio.gather(
        *(_run(provider, fetcher) for provider, fetcher in fetchers.items())
    )
    payloads: list[CryptoInsightSnapshotUpsert] = []
    for result in results:
        payloads.extend(_metric_to_payload(metric) for metric in result.metrics)
        warnings.extend(result.warnings)
    return CryptoInsightBuildResult(payloads=tuple(payloads), warnings=tuple(warnings))


def build_result_to_dict(result: CryptoInsightBuildResult) -> dict[str, Any]:
    return {
        "payloads": len(result.payloads),
        "warnings": list(result.warnings),
        "metrics": [
            {
                "metric": payload.metric,
                "provider": payload.provider,
                "symbol": payload.symbol,
                "snapshot_at": payload.snapshot_at.isoformat(),
            }
            for payload in result.payloads
        ],
    }

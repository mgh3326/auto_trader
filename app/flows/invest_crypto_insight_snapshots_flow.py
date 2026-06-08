"""Prefect wrapper for crypto_insight_snapshots refresh (ROB-452 follow-up).

get_crypto_market_regime / get_crypto_catalysts (ROB-452 P1) read
``crypto_insight_snapshots`` (Fear&Greed / DeFi TVL / stablecoin supply / breadth).
That snapshot has a CLI + job but **no Prefect flow** — so without a daily schedule it
goes stale (the regime tool's tvl/stablecoin/breadth fields drop to "missing"). This is
the missing daily build trigger (mirrors the crypto/KR/US screener-snapshot flows).

Default providers extend the build's DEFAULT_PROVIDERS (which only populate fng /
dominance / funding) with the keyless DeFi providers (defillama/tradingview) so the
regime tool's tvl/stablecoin/breadth fields actually populate. coinglass/tokenomist are
key-gated PoCs and are left out by default (they fail-open with a warning when no key).

Importable only; no deployment is registered here. Writes are runtime-gated by
``INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED`` (the shared snapshot-commit gate) so an
accidental manual run stays dry-run unless the operator enables the Prefect worker env.
"""

from __future__ import annotations

from typing import Any

from prefect import flow, task

from app.core.config import settings
from app.jobs.crypto_insight_snapshots import refresh_crypto_insight_snapshots
from app.services.crypto_insight_snapshots.builder import DEFAULT_PROVIDERS

# Extend the build's DEFAULT_PROVIDERS (fng / dominance / funding) with the keyless DeFi
# providers the regime tool needs populated. DEFAULT_PROVIDERS is imported (not re-listed)
# so this flow file carries no venue-name string literals — keeps the ROB-285 audit clean.
# coinglass/tokenomist are key-gated PoCs → left out (they fail-open without a key).
_DEFAULT_FLOW_PROVIDERS: tuple[str, ...] = (
    *DEFAULT_PROVIDERS,
    "defillama",  # tvl (per-chain) + stablecoin supply
    "tradingview",  # crypto breadth (reference)
)


async def run_crypto_insight_refresh(
    *,
    providers: tuple[str, ...] | list[str] | None = None,
    symbols: tuple[str, ...] | list[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Run the crypto-insight snapshot refresh with env-gated commit behavior.

    ``commit`` derives from ``INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED`` (default
    ``False`` → dry-run). The job requires ``confirm=True`` when ``dry_run=False``, so
    both are wired from the same gate — the build never persists when the gate is off.
    """
    commit_enabled = bool(settings.invest_screener_snapshots_commit_enabled)
    result = await refresh_crypto_insight_snapshots(
        dry_run=not commit_enabled,
        confirm=commit_enabled,
        providers=providers or _DEFAULT_FLOW_PROVIDERS,
        symbols=symbols,
        limit=limit,
    )
    return {
        "committed": result.committed,
        "providers": list(result.providers),
        "dry_run": not commit_enabled,
    }


@task(name="invest_crypto_insight_snapshots_refresh")
async def invest_crypto_insight_snapshots_task(
    *,
    providers: tuple[str, ...] | list[str] | None = None,
    symbols: tuple[str, ...] | list[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    return await run_crypto_insight_refresh(
        providers=providers, symbols=symbols, limit=limit
    )


@flow(name="invest_crypto_insight_snapshots")
async def invest_crypto_insight_snapshots_flow(
    *,
    providers: tuple[str, ...] | list[str] | None = None,
    symbols: tuple[str, ...] | list[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Daily crypto-insight snapshot refresh; deployment registration deferred."""
    return await invest_crypto_insight_snapshots_task(
        providers=providers, symbols=symbols, limit=limit
    )


from __future__ import annotations

import asyncio
import json
import os

from app.jobs.crypto_insight_snapshots import refresh_crypto_insight_snapshots
from app.services.crypto_insight_snapshots.builder import DEFAULT_PROVIDERS

DEFAULT_FLOW_PROVIDERS = [*DEFAULT_PROVIDERS, "defillama", "tradingview"]


def _json_env(name: str):
    value = os.getenv(name)
    if not value:
        return None
    return json.loads(value)


def _sample_payload(sample) -> dict[str, object]:
    return {
        "metric": sample.metric,
        "provider": sample.provider,
        "symbol": sample.symbol,
        "value": str(sample.value) if sample.value is not None else None,
        "unit": sample.unit,
        "snapshot_at": sample.snapshot_at.isoformat(),
    }


async def main() -> None:
    providers = _json_env("CRYPTO_INSIGHT_PROVIDERS_JSON")
    symbols = _json_env("CRYPTO_INSIGHT_SYMBOLS_JSON")
    limit_value = os.getenv("CRYPTO_INSIGHT_LIMIT")
    limit = int(limit_value) if limit_value else None
    commit_enabled = os.getenv("CRYPTO_INSIGHT_COMMIT_ENABLED", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    effective_providers = providers or list(DEFAULT_FLOW_PROVIDERS)
    result = await refresh_crypto_insight_snapshots(
        dry_run=not commit_enabled,
        confirm=commit_enabled,
        providers=effective_providers,
        symbols=symbols,
        limit=limit,
    )
    payload = {
        "snapshots_built": result.snapshots_built,
        "committed": result.committed,
        "started_at": result.started_at.isoformat(),
        "finished_at": result.finished_at.isoformat(),
        "providers": list(result.providers),
        "snapshot_at_distribution": result.snapshot_at_distribution,
        "warnings": list(result.warnings),
        "samples": [_sample_payload(sample) for sample in result.samples],
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


asyncio.run(main())

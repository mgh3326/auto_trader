import datetime as dt
from decimal import Decimal

import pytest

from app.services.crypto_insight_snapshots.builder import build_crypto_insight_snapshots
from app.services.external.crypto_insights import (
    CryptoInsightMetric,
    CryptoInsightProviderResult,
)

pytestmark = pytest.mark.asyncio


async def test_builder_converts_provider_metrics_to_snapshot_payloads():
    observed_at = dt.datetime(2026, 5, 13, tzinfo=dt.UTC)

    async def fake_provider():
        return CryptoInsightProviderResult(
            metrics=(
                CryptoInsightMetric(
                    metric="fear_greed",
                    provider="alternative_me",
                    symbol=None,
                    value=Decimal("70"),
                    unit="score",
                    label="Greed",
                    source_url="https://api.alternative.me/fng/",
                    observed_at=observed_at,
                    freshness_seconds=1,
                    raw_payload={"token": "synthetic"},
                ),
            ),
            warnings=("sample warning",),
        )

    result = await build_crypto_insight_snapshots(
        providers=["alternative_me"],
        provider_fetchers={"alternative_me": fake_provider},
    )

    assert len(result.payloads) == 1
    assert result.payloads[0].metric == "fear_greed"
    assert result.payloads[0].provider == "alternative_me"
    assert result.payloads[0].snapshot_at == observed_at
    assert result.payloads[0].raw_payload == {"token": "[REDACTED]"}
    assert result.warnings == ("sample warning",)


async def test_builder_reports_unknown_provider_as_warning():
    result = await build_crypto_insight_snapshots(providers=["unknown_provider"])

    assert result.payloads == ()
    assert "unsupported crypto insight provider" in result.warnings[0]


async def test_builder_uses_requested_binance_symbols(monkeypatch):
    captured = {}

    async def fake_binance(symbols, **kwargs):
        captured["symbols"] = symbols
        return CryptoInsightProviderResult()

    monkeypatch.setattr(
        "app.services.crypto_insight_snapshots.builder.fetch_binance_funding_rates",
        fake_binance,
    )

    await build_crypto_insight_snapshots(providers=["binance"], symbols=["solusdt"])

    assert captured["symbols"] == ("SOLUSDT",)

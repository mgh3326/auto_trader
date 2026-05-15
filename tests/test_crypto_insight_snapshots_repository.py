import datetime as dt
from decimal import Decimal

import pytest

from app.services.crypto_insight_snapshots.repository import (
    CryptoInsightSnapshotsRepository,
    CryptoInsightSnapshotUpsert,
    redact_sensitive_payload,
)


def _payload(value: str = "1.23", *, snapshot_at: dt.datetime | None = None):
    return CryptoInsightSnapshotUpsert(
        metric="funding_rate",
        provider="binance",
        symbol="btcusdt",
        value=Decimal(value),
        unit="ratio",
        label="longs pay shorts",
        snapshot_at=snapshot_at or dt.datetime(2026, 5, 13, tzinfo=dt.UTC),
        source_url="https://fapi.binance.com/fapi/v1/premiumIndex",
        freshness_seconds=10,
        raw_payload={"api_key": "abc123", "nested": {"Authorization": "Bearer value"}},
    )


@pytest.mark.asyncio
async def test_upsert_inserts_and_updates_existing_snapshot(db_session):
    repo = CryptoInsightSnapshotsRepository(db_session)
    inserted = await repo.upsert([_payload("1.23")])
    updated = await repo.upsert([_payload("2.34")])
    await db_session.commit()

    latest = await repo.get_latest("funding_rate", provider="binance", symbol="BTCUSDT")

    assert inserted == 1
    assert updated == 1
    assert latest is not None
    assert latest.value == Decimal("2.3400000000")
    assert latest.raw_payload["api_key"] == "[REDACTED]"
    assert latest.raw_payload["nested"]["Authorization"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_list_latest_returns_newest_per_metric_provider_symbol(db_session):
    repo = CryptoInsightSnapshotsRepository(db_session)
    older = dt.datetime(2026, 5, 12, tzinfo=dt.UTC)
    newer = dt.datetime(2026, 5, 13, tzinfo=dt.UTC)
    await repo.upsert(
        [_payload("1.0", snapshot_at=older), _payload("2.0", snapshot_at=newer)]
    )
    await db_session.commit()

    rows = await repo.list_latest(metrics=["funding_rate"], providers=["binance"])

    assert len(rows) == 1
    assert rows[0].snapshot_at == newer
    assert rows[0].symbol == "BTCUSDT"


def test_redact_sensitive_payload_bounds_and_redacts():
    redacted = redact_sensitive_payload(
        {
            "token_value": "secret-token",
            "safe": "x" * 700,
            "items": list(range(100)),
        }
    )

    assert redacted is not None
    assert redacted["token_value"] == "[REDACTED]"
    assert len(redacted["safe"]) == 500
    assert len(redacted["items"]) == 64

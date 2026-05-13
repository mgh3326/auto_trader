import datetime as dt
from decimal import Decimal

import pytest

from app.jobs.crypto_insight_snapshots import refresh_crypto_insight_snapshots
from app.services.crypto_insight_snapshots.builder import (
    DEFAULT_PROVIDERS,
    CryptoInsightBuildResult,
)
from app.services.crypto_insight_snapshots.repository import CryptoInsightSnapshotUpsert

pytestmark = pytest.mark.asyncio


def _payload():
    return CryptoInsightSnapshotUpsert(
        metric="fear_greed",
        provider="alternative_me",
        value=Decimal("70"),
        unit="score",
        label="Greed",
        snapshot_at=dt.datetime(2026, 5, 13, tzinfo=dt.UTC),
        source_url="https://api.alternative.me/fng/",
        raw_payload={"value": "70"},
    )


async def test_refresh_crypto_insight_snapshots_dry_run_does_not_commit(monkeypatch):
    committed = False

    async def fake_build(**kwargs):
        return CryptoInsightBuildResult(payloads=(_payload(),), warnings=("ok",))

    async def fake_commit(payloads):
        nonlocal committed
        committed = True

    monkeypatch.setattr(
        "app.jobs.crypto_insight_snapshots.build_crypto_insight_snapshots", fake_build
    )
    monkeypatch.setattr(
        "app.jobs.crypto_insight_snapshots._commit_payloads", fake_commit
    )

    result = await refresh_crypto_insight_snapshots(
        dry_run=True, providers=["alternative_me"]
    )

    assert result.snapshots_built == 1
    assert result.committed is False
    assert committed is False
    assert result.warnings == ("ok",)


async def test_refresh_crypto_insight_snapshots_requires_confirm_for_write(monkeypatch):
    async def fake_build(**kwargs):
        return CryptoInsightBuildResult(payloads=(_payload(),), warnings=())

    monkeypatch.setattr(
        "app.jobs.crypto_insight_snapshots.build_crypto_insight_snapshots", fake_build
    )

    with pytest.raises(ValueError, match="confirm=True"):
        await refresh_crypto_insight_snapshots(dry_run=False, confirm=False)


async def test_refresh_crypto_insight_snapshots_commits_when_confirmed(monkeypatch):
    committed_count = 0

    async def fake_build(**kwargs):
        return CryptoInsightBuildResult(payloads=(_payload(),), warnings=())

    async def fake_commit(payloads):
        nonlocal committed_count
        committed_count = len(payloads)

    monkeypatch.setattr(
        "app.jobs.crypto_insight_snapshots.build_crypto_insight_snapshots", fake_build
    )
    monkeypatch.setattr(
        "app.jobs.crypto_insight_snapshots._commit_payloads", fake_commit
    )

    result = await refresh_crypto_insight_snapshots(dry_run=False, confirm=True)

    assert result.committed is True
    assert committed_count == 1
    assert result.providers == DEFAULT_PROVIDERS

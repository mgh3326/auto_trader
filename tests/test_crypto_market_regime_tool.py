"""ROB-452 P1: get_crypto_market_regime handler (hermetic — repo + session mocked)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.mcp_server.tooling.fundamentals import _crypto_regime as mod

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _snap(
    metric,
    provider,
    value,
    *,
    symbol=None,
    age_seconds=60,
    freshness=None,
    unit=None,
    label=None,
):
    return SimpleNamespace(
        metric=metric,
        provider=provider,
        symbol=symbol,
        value=Decimal(str(value)) if value is not None else None,
        unit=unit,
        label=label,
        freshness_seconds=freshness,
        snapshot_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=age_seconds),
    )


def _patch(monkeypatch, *, get_latest, list_latest):
    monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(mod, "get_latest_crypto_insight", get_latest)
    monkeypatch.setattr(mod, "list_latest_crypto_insights", list_latest)


async def test_fng_fresh_others_missing_oi_disabled(monkeypatch):
    async def get_latest(session, metric, *, provider=None, symbol=None):
        if metric == "fear_greed":
            return _snap(
                "fear_greed", "alternative_me", 72, label="Greed", unit="score"
            )
        return None  # stablecoin / breadth missing

    async def list_latest(session, *, metrics=None, providers=None, **kw):
        return []  # tvl missing

    _patch(monkeypatch, get_latest=get_latest, list_latest=list_latest)
    out = await mod.handle_get_crypto_market_regime()

    regime = out["regime"]
    assert regime["fng"]["state"] == "fresh"
    assert regime["fng"]["value"] == pytest.approx(72.0)
    assert regime["fng"]["label"] == "Greed"
    assert regime["stablecoin_supply"]["state"] == "missing"
    assert regime["breadth"]["state"] == "missing"
    assert regime["tvl"]["state"] == "missing"
    assert regime["tvl"]["by_protocol"] == []
    # coinglass PoC is disabled — honest, not a fake value
    assert regime["aggregate_oi"]["state"] == "disabled"
    assert regime["aggregate_oi"]["value"] is None
    assert out["source"] == "crypto_insight_snapshots"


async def test_fng_stale_when_old(monkeypatch):
    async def get_latest(session, metric, *, provider=None, symbol=None):
        if metric == "fear_greed":
            # 2 days old, no tighter freshness → past the 24h regime threshold
            return _snap("fear_greed", "alternative_me", 30, age_seconds=2 * 24 * 3600)
        return None

    async def list_latest(session, *, metrics=None, providers=None, **kw):
        return []

    _patch(monkeypatch, get_latest=get_latest, list_latest=list_latest)
    out = await mod.handle_get_crypto_market_regime()
    assert out["regime"]["fng"]["state"] == "stale"


async def test_tvl_per_protocol_when_present(monkeypatch):
    async def get_latest(session, metric, *, provider=None, symbol=None):
        return None

    async def list_latest(session, *, metrics=None, providers=None, **kw):
        return [
            _snap("tvl", "defillama", 900_000_000_000, symbol="BTC", unit="usd"),
            _snap("tvl", "defillama", 500_000_000_000, symbol="ETH", unit="usd"),
        ]

    _patch(monkeypatch, get_latest=get_latest, list_latest=list_latest)
    out = await mod.handle_get_crypto_market_regime()
    tvl = out["regime"]["tvl"]
    assert tvl["state"] == "fresh"
    syms = {p["symbol"] for p in tvl["by_protocol"]}
    assert syms == {"BTC", "ETH"}  # honest per-protocol, no fabricated global total


async def test_db_error_returns_structured_payload(monkeypatch):
    async def get_latest(session, metric, *, provider=None, symbol=None):
        raise RuntimeError("db down")

    async def list_latest(session, *, metrics=None, providers=None, **kw):
        return []

    _patch(monkeypatch, get_latest=get_latest, list_latest=list_latest)
    out = await mod.handle_get_crypto_market_regime()
    assert "error" in out  # fail-open: structured error, not a raise

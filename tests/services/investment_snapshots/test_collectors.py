"""ROB-269 Phase 2 — collector protocol + registry."""

from __future__ import annotations

import datetime as dt

import pytest

from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectorProtocol,
    SnapshotCollectorRegistry,
    SnapshotCollectResult,
    default_collector_registry,
)


class _FakeMarketCollector:
    snapshot_kind = "market"

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        return [
            SnapshotCollectResult(
                snapshot_kind="market",
                market=request.market,
                source_kind="manual",
                payload_json={"kospi": 2710.0},
                as_of=dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC),
            )
        ]


class _FakePortfolioCollector:
    snapshot_kind = "portfolio"

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        return [
            SnapshotCollectResult(
                snapshot_kind="portfolio",
                market=request.market,
                account_scope=request.account_scope,
                source_kind="manual",
                payload_json={"cash_krw": 1_000_000, "holdings": []},
                as_of=dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC),
            )
        ]


def test_default_registry_is_empty():
    reg = default_collector_registry()
    assert len(reg) == 0
    assert reg.list_kinds() == set()


def test_register_and_lookup():
    reg = SnapshotCollectorRegistry()
    market = _FakeMarketCollector()
    reg.register(market)
    assert reg.get("market") is market
    assert reg.list_kinds() == {"market"}


def test_register_duplicate_kind_raises():
    reg = SnapshotCollectorRegistry()
    reg.register(_FakeMarketCollector())
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_FakeMarketCollector())


def test_register_two_different_kinds():
    reg = SnapshotCollectorRegistry()
    reg.register(_FakeMarketCollector())
    reg.register(_FakePortfolioCollector())
    assert reg.list_kinds() == {"market", "portfolio"}
    assert reg.get("market") is not reg.get("portfolio")


def test_get_missing_kind_returns_none():
    reg = SnapshotCollectorRegistry()
    assert reg.get("never_registered") is None


def test_fake_collector_satisfies_protocol():
    # ``runtime_checkable`` means isinstance check works.
    assert isinstance(_FakeMarketCollector(), SnapshotCollectorProtocol)


def test_snapshot_collect_result_source_ref_triple_enforced():
    # Half-set triple is rejected.
    with pytest.raises(ValueError, match="all be set or all None"):
        SnapshotCollectResult(
            snapshot_kind="market",
            market="kr",
            source_kind="manual",
            source_table="some_table",
            source_id=None,
            source_uri=None,
            payload_json={},
            as_of=dt.datetime(2026, 5, 19, tzinfo=dt.UTC),
        )


def test_snapshot_collect_result_domain_ref_requires_triple():
    with pytest.raises(ValueError, match="domain_ref"):
        SnapshotCollectResult(
            snapshot_kind="market",
            market="kr",
            source_kind="domain_ref",
            source_table=None,
            source_id=None,
            source_uri=None,
            payload_json={},
            as_of=dt.datetime(2026, 5, 19, tzinfo=dt.UTC),
        )


@pytest.mark.asyncio
async def test_collector_round_trip_via_protocol():
    market = _FakeMarketCollector()
    request = CollectorRequest(
        market="kr",
        account_scope="kis_live",
        policy_snapshot={"policy_version": "intraday_action_report_v1"},
    )
    results = await market.collect(request)
    assert len(results) == 1
    assert results[0].snapshot_kind == "market"
    assert results[0].payload_json == {"kospi": 2710.0}

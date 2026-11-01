"""Broker-backed bar sources (ROB-1448): broker preferred, public fallback on
failure, the manifest names the serving source, raw and adjusted vintages
stored apart."""

from __future__ import annotations

import json
from datetime import date

import pytest

from research.data_plane.bar_sources import SourceBar
from research.data_plane.collect import CollectError, collect
from research.data_plane.contract import (
    DATASET_BARS,
    DATASET_BARS_ADJUSTED,
    DATASET_UNIVERSE,
)
from research.data_plane.store import manifest_path, partition_path, read_partition

from .data_plane_fixtures import (
    FakeBrokerClient,
    FakeClock,
    FakeSources,
    evening,
    fake_budgets,
)

DAY = date(2026, 10, 30)
BARS = [DATASET_BARS, DATASET_BARS_ADJUSTED]


def _universe(root, market="kr", day=DAY):
    collect(
        root,
        market=market,
        day=day,
        datasets=[DATASET_UNIVERSE],
        fetch=FakeSources(),
        now=lambda: evening(market, day),
        commit="t",
    )


def _bars(root, src, market="kr", day=DAY, symbols=None, **kw):
    kw.setdefault("budgets", fake_budgets())
    return collect(
        root,
        market=market,
        day=day,
        datasets=BARS,
        fetch=src,
        now=lambda: evening(market, day),
        symbols=symbols,
        commit="t",
        **kw,
    )


def _record(root, dataset, market="kr"):
    return json.loads(manifest_path(root, dataset, market).read_text().splitlines()[-1])


@pytest.mark.unit
def test_broker_source_is_preferred_and_named_in_the_manifest(tmp_path):
    _universe(tmp_path)
    src, kis = FakeSources(), FakeBrokerClient()
    results = _bars(tmp_path, src, broker_clients={"kis": kis})

    assert [r.action for r in results] == ["written", "written"]
    assert src.calls["query2.finance.yahoo.com"] == 0  # the fallback was not needed
    raw = read_partition(tmp_path, DATASET_BARS, "kr", DAY)
    assert {r["source"] for r in raw} == {"kis_daily_chart"}
    assert {r["price_basis"] for r in raw} == {"raw"}
    rec = _record(tmp_path, DATASET_BARS)
    assert rec["served_by"] == "kis"
    assert rec["source_order"] == ["kis", "toss", "yahoo"]
    assert rec["source_attempts"] == [
        {"source": "kis", "outcome": "served", "coverage": 1.0}
    ]
    assert rec["adjustment_basis"].startswith("kis FID_ORG_ADJ_PRC=1")
    assert rec["research_budget"] == {"kis": "2/1s"}
    # KR raw: one bar ending on the partition day, unadjusted
    raw_calls = [c for c in kis.calls if not c["adjusted"]]
    assert {(c["end"], c["count"]) for c in raw_calls} == {(DAY, 1)}


@pytest.mark.unit
def test_failing_broker_falls_back_to_the_public_source(tmp_path):
    _universe(tmp_path)
    src = FakeSources()
    kis = FakeBrokerClient(fail_all=True)
    results = _bars(
        tmp_path,
        src,
        broker_clients={"kis": kis},
        source_unavailable={"toss": "client_unavailable:TossApiDisabled"},
    )

    assert [r.action for r in results] == ["written", "written"]
    raw = read_partition(tmp_path, DATASET_BARS, "kr", DAY)
    assert {r["source"] for r in raw} == {"yahoo_chart"}
    rec = _record(tmp_path, DATASET_BARS)
    assert rec["served_by"] == "yahoo"
    assert rec["source_attempts"] == [
        {
            "source": "kis",
            "outcome": "coverage_below_floor",
            "coverage": 0.0,
            "missing_reasons": {"client_error": 3},
        },
        {
            "source": "toss",
            "outcome": "unavailable",
            "detail": "client_unavailable:TossApiDisabled",
        },
        {"source": "yahoo", "outcome": "served", "coverage": 1.0},
    ]
    # the broker's error text never reaches the manifest (class name only)
    assert "secret-ish" not in manifest_path(tmp_path, DATASET_BARS, "kr").read_text()
    assert _record(tmp_path, DATASET_BARS_ADJUSTED)["served_by"] == "yahoo"


@pytest.mark.unit
def test_partial_broker_failure_below_floor_falls_through_to_the_next_broker(
    tmp_path,
):
    _universe(tmp_path)
    src = FakeSources()
    kis = FakeBrokerClient(fail=frozenset({"247540"}))  # 2/3 < 0.95
    toss = FakeBrokerClient()
    _bars(tmp_path, src, broker_clients={"kis": kis, "toss": toss})
    rec = _record(tmp_path, DATASET_BARS)
    assert rec["served_by"] == "toss"
    assert [a["outcome"] for a in rec["source_attempts"]] == [
        "coverage_below_floor",
        "served",
    ]
    rows = read_partition(tmp_path, DATASET_BARS, "kr", DAY)
    # one partition, one source: the two KIS bars are not mixed in
    assert {r["source"] for r in rows} == {"toss_candles_1d"}
    assert len(rows) == 3
    assert src.calls["query2.finance.yahoo.com"] == 0


@pytest.mark.unit
def test_unconfigured_brokers_are_recorded_and_yahoo_serves(tmp_path):
    _universe(tmp_path)
    _bars(tmp_path, FakeSources())
    rec = _record(tmp_path, DATASET_BARS)
    assert rec["served_by"] == "yahoo"
    assert [(a["source"], a["outcome"]) for a in rec["source_attempts"]] == [
        ("kis", "unavailable"),
        ("toss", "unavailable"),
        ("yahoo", "served"),
    ]


@pytest.mark.unit
def test_every_source_failing_writes_nothing(tmp_path):
    _universe(tmp_path)
    with pytest.raises(CollectError, match="kis coverage 0.000.*yahoo coverage 0.000"):
        _bars(
            tmp_path,
            FakeSources(missing=frozenset({"005930", "000660", "247540"})),
            broker_clients={"kis": FakeBrokerClient(fail_all=True)},
        )
    assert not partition_path(tmp_path, DATASET_BARS, "kr", DAY).exists()
    assert not partition_path(tmp_path, DATASET_BARS_ADJUSTED, "kr", DAY).exists()


@pytest.mark.unit
def test_an_outage_stops_spending_the_budget_after_a_streak(tmp_path, monkeypatch):
    from research.data_plane import collect as collect_module

    monkeypatch.setattr(collect_module, "SOURCE_ABORT_STREAK", 2)
    _universe(tmp_path)
    kis = FakeBrokerClient(fail_all=True)
    _bars(tmp_path, FakeSources(), broker_clients={"kis": kis})
    rec = _record(tmp_path, DATASET_BARS)
    assert rec["source_attempts"][0] == {
        "source": "kis",
        "outcome": "unavailable",
        "detail": "kis: 2 consecutive errors before any bar (last client_error)",
    }
    assert len(kis.calls) == 4  # two per vintage, not the whole universe


@pytest.mark.unit
@pytest.mark.parametrize("market", ["kr", "us"])
def test_raw_and_adjusted_vintages_are_stored_separately(tmp_path, market):
    symbols = ["AAPL", "KO"] if market == "us" else None
    _universe(tmp_path, market)
    kis = FakeBrokerClient()
    _bars(
        tmp_path,
        FakeSources(),
        market=market,
        symbols=symbols,
        broker_clients={"kis": kis},
        adjusted_window=5,
    )
    raw_path = partition_path(tmp_path, DATASET_BARS, market, DAY)
    adj_path = partition_path(tmp_path, DATASET_BARS_ADJUSTED, market, DAY)
    assert raw_path != adj_path and raw_path.exists() and adj_path.exists()

    raw = read_partition(tmp_path, DATASET_BARS, market, DAY)
    adj = read_partition(tmp_path, DATASET_BARS_ADJUSTED, market, DAY)
    instruments = {r["instrument"] for r in raw}
    # raw: exactly the partition day's bar, as traded
    assert all(r["bar_date"] == DAY.isoformat() for r in raw)
    assert {r["close"] for r in raw} == {"100"}
    assert {r["price_basis"] for r in raw} == {"raw"}
    # adjusted: the last 5 sessions as published on DAY (2:1 split on DAY)
    assert len(adj) == 5 * len(instruments)
    for inst in instruments:
        mine = sorted(
            (r for r in adj if r["instrument"] == inst), key=lambda r: r["bar_date"]
        )
        assert [r["close"] for r in mine] == ["50", "50", "50", "50", "100"]
        assert mine[-1]["bar_date"] == DAY.isoformat()
    assert {r["price_basis"] for r in adj} == {"adjusted"}
    assert {r["vintage_date"] for r in adj} == {DAY.isoformat()}
    # every adjusted row is available on the vintage day, whatever its bar date
    assert {r["available_at"] for r in adj} == {raw[0]["available_at"]}

    raw_rec = _record(tmp_path, DATASET_BARS, market)
    adj_rec = _record(tmp_path, DATASET_BARS_ADJUSTED, market)
    assert (raw_rec["vintage"], adj_rec["vintage"]) == ("raw", "adjusted")
    expect_raw = "FID_ORG_ADJ_PRC=1" if market == "kr" else "MODP=0"
    expect_adj = "FID_ORG_ADJ_PRC=0" if market == "kr" else "MODP=1"
    assert expect_raw in raw_rec["adjustment_basis"]
    assert expect_adj in adj_rec["adjustment_basis"]
    assert adj_rec["adjusted_window_sessions"] == 5
    assert {c["adjusted"] for c in kis.calls} == {True, False}


@pytest.mark.unit
def test_yahoo_adjusted_vintage_is_derived_and_labelled(tmp_path):
    _universe(tmp_path)
    _bars(tmp_path, FakeSources(), adjusted_window=3)
    rec = _record(tmp_path, DATASET_BARS_ADJUSTED)
    assert rec["served_by"] == "yahoo"
    assert "(derived)" in rec["adjustment_basis"]
    adj = read_partition(tmp_path, DATASET_BARS_ADJUSTED, "kr", DAY)
    assert len(adj) == 3 * 3
    assert {r["close"] for r in adj} == {"105.5"}  # fixture adjclose == close


@pytest.mark.unit
def test_every_broker_call_takes_a_budget_slot(tmp_path):
    _universe(tmp_path)
    clock = FakeClock()
    budgets = fake_budgets(clock)
    kis = FakeBrokerClient()
    _bars(tmp_path, FakeSources(), broker_clients={"kis": kis}, budgets=budgets)
    assert budgets["kis"].acquired == len(kis.calls) == 6
    assert clock.slept >= 2.0  # 6 calls at 2/s needed at least two waits


@pytest.mark.unit
def test_a_bar_dated_after_the_partition_is_dropped_not_misfiled(tmp_path):
    class Future:
        def daily_bars(self, *, market, instrument, venue, end, count, adjusted):
            return [SourceBar(date(2026, 11, 2), "1", "1", "1", "1", 1)]

    _universe(tmp_path)
    _bars(tmp_path, FakeSources(), broker_clients={"kis": Future()})
    rec = _record(tmp_path, DATASET_BARS)
    assert rec["served_by"] == "yahoo"
    assert rec["source_attempts"][0]["missing_reasons"] == {"no_bar_for_session": 3}


@pytest.mark.unit
def test_crypto_refuses_bar_sources_and_the_adjusted_dataset(tmp_path):
    with pytest.raises(ValueError, match="do not apply to crypto"):
        collect(
            tmp_path,
            market="crypto",
            day=DAY,
            datasets=[DATASET_BARS_ADJUSTED],
            fetch=FakeSources(),
            commit="t",
        )
    with pytest.raises(ValueError, match="do not apply to crypto"):
        collect(
            tmp_path,
            market="crypto",
            day=DAY,
            fetch=FakeSources(),
            broker_clients={"kis": FakeBrokerClient()},
            commit="t",
        )

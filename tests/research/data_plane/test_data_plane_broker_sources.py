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
            # stopped at the first error: 2/3 can no longer reach 0.95
            "source": "kis",
            "outcome": "coverage_below_floor",
            "coverage": 0.666667,
            "instruments_tried": 1,
            "missing_reasons": {"client_error": 1},
        },
        {
            "source": "toss",
            "outcome": "unavailable",
            "detail": "client_unavailable:TossApiDisabled",
        },
        {"source": "yahoo", "outcome": "served", "coverage": 1.0},
    ]
    assert len(kis.calls) == 2  # one per vintage: the budget is not spent further
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
    with pytest.raises(
        CollectError, match="kis coverage 0.667 < 0.95 after 1 .*yahoo coverage 0.667"
    ):
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
    # floor 0: only the error streak can stop the broker
    _bars(tmp_path, FakeSources(), broker_clients={"kis": kis}, min_coverage=0.0)
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
    assert rec["source_attempts"][0]["missing_reasons"] == {"no_bar_for_session": 1}


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


@pytest.mark.unit
def test_an_adjusted_vintage_is_refused_after_its_day_but_raw_backfills(tmp_path):
    """Read later, the source's adjustment would carry events announced after
    the partition day under that day's available_at."""
    from datetime import timedelta

    from research.data_plane.contract import RefusedRowError

    _universe(tmp_path)
    kis = FakeBrokerClient()
    late = evening("kr", DAY) + timedelta(days=3)
    with pytest.raises(RefusedRowError, match="bars_1d_adjusted .* only be observed"):
        collect(
            tmp_path,
            market="kr",
            day=DAY,
            datasets=[DATASET_BARS_ADJUSTED],
            fetch=FakeSources(),
            now=lambda: late,
            broker_clients={"kis": kis},
            budgets=fake_budgets(),
            commit="t",
        )
    assert kis.calls == []  # refused before any broker call
    assert not partition_path(tmp_path, DATASET_BARS_ADJUSTED, "kr", DAY).exists()
    results = collect(
        tmp_path,
        market="kr",
        day=DAY,
        datasets=[DATASET_BARS],
        fetch=FakeSources(),
        now=lambda: late,
        broker_clients={"kis": kis},
        budgets=fake_budgets(),
        commit="t",
    )
    assert [r.action for r in results] == ["written"]


@pytest.mark.unit
def test_a_below_floor_source_never_fills_the_winners_gaps(tmp_path):
    _universe(tmp_path)
    # KIS covers only 005930 (1/3 < 0.6); Yahoo misses exactly that one (2/3)
    kis = FakeBrokerClient(fail=frozenset({"000660", "247540"}))
    _bars(
        tmp_path,
        FakeSources(missing=frozenset({"005930"})),
        broker_clients={"kis": kis},
        min_coverage=0.6,
    )
    rows = read_partition(tmp_path, DATASET_BARS, "kr", DAY)
    assert {r["source"] for r in rows} == {"yahoo_chart"}
    assert sorted(r["instrument"] for r in rows) == ["000660", "247540"]
    rec = _record(tmp_path, DATASET_BARS)
    assert rec["served_by"] == "yahoo"
    assert [m["instrument"] for m in rec["missing"]] == ["005930"]
    adj = read_partition(tmp_path, DATASET_BARS_ADJUSTED, "kr", DAY)
    assert {r["source"] for r in adj} == {"yahoo_chart"}
    assert "005930" not in {r["instrument"] for r in adj}


class _ClosingClient(FakeBrokerClient):
    def __init__(self):
        super().__init__()
        self.closed = 0

    def close(self):
        self.closed += 1


@pytest.mark.unit
def test_cli_closes_broker_clients_and_a_rerun_builds_none(
    tmp_path, monkeypatch, capsys
):
    from research.data_plane import broker_adapters, cli
    from research.data_plane import collect as collect_module

    from .test_data_plane_collect import _FrozenDatetime

    opened = []

    def fake_open(names):
        clients = {n: _ClosingClient() for n in names}
        opened.append(clients)
        return broker_adapters.BrokerClients(clients=clients)

    monkeypatch.setattr(broker_adapters, "open_broker_clients", fake_open)
    monkeypatch.setattr(cli, "make_public_fetch", lambda **_: FakeSources())
    monkeypatch.setattr(cli, "datetime", _FrozenDatetime)
    monkeypatch.setattr(collect_module, "datetime", _FrozenDatetime)
    _FrozenDatetime.frozen = evening("kr", DAY)
    argv = [
        "collect",
        "--root",
        str(tmp_path),
        "--market",
        "kr",
        "--date",
        DAY.isoformat(),
    ]
    argv += ["--broker-budget", "kis=5/1,toss=1/1"]

    assert cli.main(argv) == 0
    assert [sorted(c) for c in opened] == [["kis", "toss"]]
    assert all(c.closed == 1 for c in opened[0].values())
    assert _record(tmp_path, DATASET_BARS)["served_by"] == "kis"
    assert (tmp_path / ".budget" / "kis.json").exists()

    assert cli.main(argv) == 0  # every partition present: no client is built
    assert len(opened) == 1
    assert "already_present bars_1d kr" in capsys.readouterr().out


@pytest.mark.unit
def test_yahoo_lookback_covers_a_long_adjusted_window(tmp_path):
    _universe(tmp_path)
    _bars(tmp_path, FakeSources(), adjusted_window=100)
    adj = read_partition(tmp_path, DATASET_BARS_ADJUSTED, "kr", DAY)
    per = {r["instrument"] for r in adj}
    assert len(adj) == 100 * len(per)


@pytest.mark.unit
def test_a_past_day_rerun_builds_no_client_for_the_adjusted_vintage(
    tmp_path, monkeypatch
):
    """#2255 review: an adjusted vintage is refused after its day, so a
    backfill whose raw bars exist must not build broker clients for it."""
    from datetime import timedelta

    from research.data_plane import broker_adapters, cli
    from research.data_plane import collect as collect_module

    from .test_data_plane_collect import _FrozenDatetime

    opened = []

    def fake_open(names):
        opened.append(names)
        return broker_adapters.BrokerClients(
            clients={n: _ClosingClient() for n in names}
        )

    monkeypatch.setattr(broker_adapters, "open_broker_clients", fake_open)
    monkeypatch.setattr(cli, "make_public_fetch", lambda **_: FakeSources())
    monkeypatch.setattr(cli, "datetime", _FrozenDatetime)
    monkeypatch.setattr(collect_module, "datetime", _FrozenDatetime)
    base = ["collect", "--root", str(tmp_path), "--market", "kr"]
    base += ["--date", DAY.isoformat(), "--broker-budget", "kis=5/1,toss=1/1"]

    _FrozenDatetime.frozen = evening("kr", DAY)
    assert cli.main(base + ["--datasets", "universe,bars_1d,corporate_actions"]) == 0
    assert len(opened) == 1

    _FrozenDatetime.frozen = evening("kr", DAY) + timedelta(days=3)
    assert cli.main(base) == 3  # the adjusted vintage is refused (past day)
    assert len(opened) == 1  # and no broker client was built for it
    assert not partition_path(tmp_path, DATASET_BARS_ADJUSTED, "kr", DAY).exists()


@pytest.mark.unit
def test_an_adjusted_run_that_crosses_local_midnight_is_refused(tmp_path):
    """CodeRabbit #2255: vintages read after midnight must not be published
    under the partition day's available_at."""
    from datetime import timedelta

    from research.data_plane.contract import RefusedRowError

    _universe(tmp_path)
    clock = {"now": evening("kr", DAY)}

    class MidnightBroker(FakeBrokerClient):
        def daily_bars(self, **kw):
            out = super().daily_bars(**kw)
            # the first read already ends after the KST midnight
            clock["now"] = evening("kr", DAY) + timedelta(hours=7)
            return out

    with pytest.raises(RefusedRowError, match="bars_1d_adjusted .* only be observed"):
        collect(
            tmp_path,
            market="kr",
            day=DAY,
            datasets=[DATASET_BARS_ADJUSTED],
            fetch=FakeSources(),
            now=lambda: clock["now"],
            broker_clients={"kis": MidnightBroker()},
            budgets=fake_budgets(),
            commit="t",
        )
    assert not partition_path(tmp_path, DATASET_BARS_ADJUSTED, "kr", DAY).exists()


@pytest.mark.unit
def test_short_adjusted_windows_are_counted_in_the_manifest(tmp_path):
    class Short(FakeBrokerClient):
        def daily_bars(self, **kw):
            out = super().daily_bars(**kw)
            return out[-1:] if kw["instrument"] == "247540" else out

    _universe(tmp_path)
    _bars(tmp_path, FakeSources(), broker_clients={"kis": Short()}, adjusted_window=5)
    rec = _record(tmp_path, DATASET_BARS_ADJUSTED)
    assert rec["served_by"] == "kis"
    assert rec["instruments_short_window"] == 1

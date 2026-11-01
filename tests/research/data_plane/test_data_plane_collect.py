"""Collectors are idempotent per day, append-only, and refuse non-PIT rows."""

from __future__ import annotations

import gzip
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from research.data_plane.collect import CollectError, collect
from research.data_plane.contract import (
    DATASET_ACTIONS,
    DATASET_BARS,
    DATASET_UNIVERSE,
    DATASETS,
    RefusedRowError,
    make_row,
    validate_rows,
)
from research.data_plane.store import (
    dataset_lock,
    manifest_path,
    partition_path,
    publish_partition,
    read_partition,
)

from .data_plane_fixtures import FakeSources, evening, tree_state

# (market, first partition day, next partition day, symbols subset)
CASES = [
    ("kr", date(2026, 10, 30), date(2026, 11, 2), None),
    ("us", date(2026, 10, 30), date(2026, 11, 2), ["AAPL", "KO"]),
    ("crypto", date(2026, 10, 30), date(2026, 10, 31), None),
]


def _run(
    root: Path,
    market: str,
    day: date,
    src: FakeSources,
    symbols=None,
    datasets=DATASETS,
):
    return collect(
        root,
        market=market,
        day=day,
        datasets=datasets,
        fetch=src,
        now=lambda: evening(market, day),
        symbols=symbols,
        commit="test-commit",
    )


@pytest.mark.unit
@pytest.mark.parametrize(("market", "day", "_next", "symbols"), CASES)
def test_second_run_same_day_fetches_and_writes_nothing(
    tmp_path, market, day, _next, symbols
):
    src = FakeSources()
    first = _run(tmp_path, market, day, src, symbols)
    assert [r.action for r in first] == ["written"] * 3
    assert [r.dataset for r in first] == list(DATASETS)
    assert src.total_calls > 0
    bars = read_partition(tmp_path, DATASET_BARS, market, day)
    assert bars and all(r["available_at"] <= r["observed_at"] for r in bars)
    before = tree_state(tmp_path)

    again = FakeSources()
    second = _run(tmp_path, market, day, again, symbols)

    assert [r.action for r in second] == ["already_present"] * 3
    assert again.total_calls == 0
    assert [r.sha256 for r in second] == [r.sha256 for r in first]
    assert tree_state(tmp_path) == before  # same files, same bytes, same mtimes


@pytest.mark.unit
@pytest.mark.parametrize(("market", "day", "next_day", "symbols"), CASES)
def test_next_day_only_appends(tmp_path, market, day, next_day, symbols):
    _run(tmp_path, market, day, FakeSources(), symbols)
    before = tree_state(tmp_path)
    manifests_before = {
        ds: manifest_path(tmp_path, ds, market).read_bytes() for ds in DATASETS
    }

    results = _run(tmp_path, market, next_day, FakeSources(), symbols)

    assert [r.action for r in results] == ["written"] * 3
    after = tree_state(tmp_path)
    for name, state in before.items():
        if name.endswith("manifest.jsonl"):
            continue
        assert after[name] == state, f"{name} changed"
    for ds, old in manifests_before.items():
        new = manifest_path(tmp_path, ds, market).read_bytes()
        assert (
            new.startswith(old) and len(new.splitlines()) == len(old.splitlines()) + 1
        )
    assert partition_path(tmp_path, DATASET_BARS, market, next_day).exists()


@pytest.mark.unit
def test_actions_record_a_dividend_once_and_listing_changes(tmp_path):
    day1, day2 = date(2026, 10, 30), date(2026, 11, 2)
    _run(tmp_path, "us", day1, FakeSources(), ["AAPL", "KO"])
    rows1 = read_partition(tmp_path, DATASET_ACTIONS, "us", day1)
    divs = [r for r in rows1 if r["action_type"] == "cash_dividend"]
    assert [(r["instrument"], r["cash_amount"]) for r in divs] == [("KO", "0.53")]
    assert divs[0]["available_at"] == divs[0]["observed_at"]  # first observation
    assert divs[0]["ex_date"] <= day1.isoformat()

    _run(tmp_path, "us", day2, FakeSources(), ["AAPL", "KO"])
    rows2 = read_partition(tmp_path, DATASET_ACTIONS, "us", day2)
    assert not [
        r for r in rows2 if r["action_type"] == "cash_dividend"
    ]  # already known

    # crypto: LUNAUSDT leaves the listing between two universe snapshots
    _run(tmp_path, "crypto", day1, FakeSources())
    _run(
        tmp_path,
        "crypto",
        day1 + timedelta(days=1),
        FakeSources(binance_pairs={"BTCUSDT": "TRADING", "ETHUSDT": "BREAK"}),
    )
    changes = {
        (r["instrument"], r["action_type"])
        for r in read_partition(
            tmp_path, DATASET_ACTIONS, "crypto", day1 + timedelta(days=1)
        )
    }
    assert changes == {
        ("LUNAUSDT", "listing_removed"),
        ("ETHUSDT", "listing_status_changed"),
    }


@pytest.mark.unit
def test_crypto_partition_holds_the_previous_utc_day_candle(tmp_path):
    day = date(2026, 10, 30)
    _run(tmp_path, "crypto", day, FakeSources())
    rows = read_partition(tmp_path, DATASET_BARS, "crypto", day)
    assert {r["instrument"] for r in rows} == {
        "BTCUSDT",
        "ETHUSDT",
        "KRW-BTC",
        "KRW-ETH",
    }
    assert {r["bar_date"] for r in rows} == {"2026-10-29"}
    assert {r["available_at"] for r in rows} == {"2026-10-30T00:05:00+00:00"}


# --- PIT refusals -----------------------------------------------------------


def _bar_row(market: str, available_at: str, observed_at: str) -> dict:
    return make_row(
        instrument="005930",
        venue="KOSPI",
        event_time="2026-10-30T06:30:00+00:00",
        available_at=available_at,
        observed_at=observed_at,
        source="yahoo_chart",
        source_version="v8",
        bar_date="2026-10-30",
        open="1",
        high="1",
        low="1",
        close="1",
        volume=1,
    )


@pytest.mark.unit
def test_row_available_after_its_partition_date_is_refused_and_nothing_written(
    tmp_path,
):
    # available 2026-10-31 00:30 KST: later than partition 2026-10-30 (KST)
    row = _bar_row("kr", "2026-10-30T15:30:00+00:00", "2026-10-30T16:00:00+00:00")
    with pytest.raises(RefusedRowError, match="later than partition date 2026-10-30"):
        validate_rows("kr", DATASET_BARS, date(2026, 10, 30), [row])
    with dataset_lock(tmp_path, DATASET_BARS, "kr"):
        with pytest.raises(RefusedRowError, match="later than partition date"):
            publish_partition(
                tmp_path,
                dataset=DATASET_BARS,
                market="kr",
                day=date(2026, 10, 30),
                rows=[row],
                meta={},
            )
    assert not partition_path(tmp_path, DATASET_BARS, "kr", date(2026, 10, 30)).exists()
    assert not manifest_path(tmp_path, DATASET_BARS, "kr").exists()


@pytest.mark.unit
def test_row_observed_before_available_or_tampered_is_refused():
    early = _bar_row("kr", "2026-10-30T07:00:00+00:00", "2026-10-30T05:00:00+00:00")
    with pytest.raises(RefusedRowError, match="before available_at"):
        validate_rows("kr", DATASET_BARS, date(2026, 10, 30), [early])
    ok = _bar_row("kr", "2026-10-30T07:00:00+00:00", "2026-10-30T09:00:00+00:00")
    validate_rows("kr", DATASET_BARS, date(2026, 10, 30), [ok])
    with pytest.raises(RefusedRowError, match="earlier partition"):
        validate_rows("kr", DATASET_BARS, date(2026, 10, 31), [ok])
    with pytest.raises(RefusedRowError, match="content_hash"):
        validate_rows("kr", DATASET_BARS, date(2026, 10, 30), [{**ok, "close": "2"}])
    with pytest.raises(RefusedRowError, match="no UTC offset"):
        validate_rows(
            "kr",
            DATASET_BARS,
            date(2026, 10, 30),
            [make_row(**{**ok, "available_at": "2026-10-30T07:00:00"})],
        )


@pytest.mark.unit
@pytest.mark.parametrize("dataset", [DATASET_UNIVERSE, DATASET_ACTIONS])
def test_snapshot_dated_before_today_is_refused(tmp_path, dataset):
    """A universe/actions observation made on 10-31 cannot be filed under 10-30."""
    day = date(2026, 10, 30)
    if dataset == DATASET_ACTIONS:
        _run(tmp_path, "kr", day, FakeSources(), datasets=[DATASET_UNIVERSE])
    before = tree_state(tmp_path)
    src = FakeSources()
    with pytest.raises(RefusedRowError, match="later than the partition date"):
        collect(
            tmp_path,
            market="kr",
            day=day,
            datasets=[dataset],
            fetch=src,
            now=lambda: evening("kr", day + timedelta(days=1)),
            commit="t",
        )
    assert src.total_calls == 0
    assert tree_state(tmp_path) == before


@pytest.mark.unit
def test_bars_before_session_close_are_refused(tmp_path):
    day = date(2026, 10, 30)
    _run(tmp_path, "kr", day, FakeSources(), datasets=[DATASET_UNIVERSE])
    with pytest.raises(RefusedRowError, match="forming bar"):
        collect(
            tmp_path,
            market="kr",
            day=day,
            datasets=[DATASET_BARS],
            fetch=FakeSources(),
            now=lambda: evening("kr", day) - timedelta(hours=4),  # 14:00 KST
            commit="t",
        )
    assert not partition_path(tmp_path, DATASET_BARS, "kr", day).exists()


@pytest.mark.unit
def test_bars_never_use_a_later_universe(tmp_path):
    _run(tmp_path, "kr", date(2026, 11, 2), FakeSources(), datasets=[DATASET_UNIVERSE])
    with pytest.raises(CollectError, match="no universe partition"):
        _run(tmp_path, "kr", date(2026, 10, 30), FakeSources(), datasets=[DATASET_BARS])


@pytest.mark.unit
def test_source_returning_only_other_days_is_missing_not_misfiled(tmp_path):
    day = date(2026, 10, 30)
    _run(tmp_path, "kr", day, FakeSources(), datasets=[DATASET_UNIVERSE])
    with pytest.raises(CollectError, match="coverage 0.000"):
        _run(tmp_path, "kr", day, FakeSources(future_bar=True), datasets=[DATASET_BARS])
    assert not partition_path(tmp_path, DATASET_BARS, "kr", day).exists()


@pytest.mark.unit
def test_coverage_floor_and_missing_list(tmp_path):
    day = date(2026, 10, 30)
    _run(tmp_path, "kr", day, FakeSources(), datasets=[DATASET_UNIVERSE])
    with pytest.raises(CollectError, match="coverage 0.667"):
        _run(
            tmp_path,
            "kr",
            day,
            FakeSources(missing=frozenset({"247540"})),
            datasets=[DATASET_BARS],
        )
    results = collect(
        tmp_path,
        market="kr",
        day=day,
        datasets=[DATASET_BARS],
        fetch=FakeSources(missing=frozenset({"247540"})),
        now=lambda: evening("kr", day),
        min_coverage=0.5,
        commit="t",
    )
    assert results[0].rows == 2
    record = json.loads(
        manifest_path(tmp_path, DATASET_BARS, "kr").read_text().splitlines()[-1]
    )
    assert record["missing"] == [
        {"instrument": "247540", "venue": "KOSDAQ", "reason": "source_error:HTTP 404"}
    ]
    assert record["coverage"] == pytest.approx(2 / 3, abs=1e-6)


@pytest.mark.unit
def test_unrecorded_partition_is_repaired_without_fetching(tmp_path):
    day = date(2026, 10, 30)
    _run(tmp_path, "crypto", day, FakeSources())
    manifest = manifest_path(tmp_path, DATASET_BARS, "crypto")
    manifest.unlink()  # simulate a crash between publish and manifest append
    src = FakeSources()
    results = _run(tmp_path, "crypto", day, src)
    assert [r.action for r in results] == [
        "already_present",
        "manifest_repaired",
        "already_present",
    ]
    assert src.total_calls == 0
    assert len(manifest.read_text().splitlines()) == 1


@pytest.mark.unit
def test_partition_bytes_are_deterministic(tmp_path):
    day = date(2026, 10, 30)
    _run(tmp_path / "a", "crypto", day, FakeSources())
    _run(tmp_path / "b", "crypto", day, FakeSources())
    a = gzip.decompress(
        partition_path(tmp_path / "a", DATASET_UNIVERSE, "crypto", day).read_bytes()
    )
    b = gzip.decompress(
        partition_path(tmp_path / "b", DATASET_UNIVERSE, "crypto", day).read_bytes()
    )
    assert a == b

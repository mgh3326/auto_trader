"""hk 2552 (#2253 review follow-ups): bad health grace, an empty dataset list,
an empty first universe, and the listing-diff reference when a universe record
is missing."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from research.data_plane.cli import build_parser, main
from research.data_plane.collect import CollectError, collect
from research.data_plane.contract import (
    DATASET_ACTIONS,
    DATASET_UNIVERSE,
)
from research.data_plane.health import check_health
from research.data_plane.store import (
    manifest_path,
    partition_path,
    read_partition,
    recorded_dates,
)

from .data_plane_fixtures import FakeSources, evening, tree_state


def _run(root, market, day, src, datasets):
    return collect(
        root,
        market=market,
        day=day,
        datasets=datasets,
        fetch=src,
        now=lambda: evening(market, day),
        commit="t",
    )


# --- --grace-hours ------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("value", ["nan", "-1", "-0.01", "inf", "-inf", "abc"])
def test_health_cli_rejects_non_finite_or_negative_grace(value, capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(
            [
                "health",
                "--root",
                "/nonexistent",
                "--market",
                "kr",
                "--dataset",
                "bars_1d",
                "--grace-hours",
                value,
            ]
        )
    assert exc.value.code == 2
    assert "--grace-hours" in capsys.readouterr().err


@pytest.mark.unit
@pytest.mark.parametrize("value", ["0", "1", "2.5"])
def test_health_cli_accepts_finite_non_negative_grace(value):
    args = build_parser().parse_args(
        ["health", "--root", "/x", "--market", "kr", "--dataset", "bars_1d"]
        + ["--grace-hours", value]
    )
    assert args.grace_hours == float(value)


@pytest.mark.unit
def test_check_health_rejects_a_negative_grace(tmp_path):
    with pytest.raises(ValueError, match="grace"):
        check_health(
            tmp_path,
            market="kr",
            dataset="bars_1d",
            now=evening("kr", date(2026, 11, 2)),
            grace=timedelta(hours=-1),
        )


# --- --datasets ---------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("value", ["", " ", ",", " , ,"])
def test_collect_cli_rejects_an_empty_dataset_list(value, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(
            ["collect", "--root", str(tmp_path), "--market", "kr"]
            + ["--datasets", value]
        )
    assert exc.value.code == 2
    assert "--datasets is empty" in capsys.readouterr().err
    assert tree_state(tmp_path) == {}


@pytest.mark.unit
def test_collect_cli_rejects_an_unknown_dataset_before_collecting(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(
            ["collect", "--root", str(tmp_path), "--market", "kr"]
            + ["--datasets", "universe,bars"]
        )
    assert "unknown datasets ['bars']" in capsys.readouterr().err
    assert tree_state(tmp_path) == {}


@pytest.mark.unit
def test_collect_rejects_an_empty_dataset_list(tmp_path):
    src = FakeSources()
    with pytest.raises(ValueError, match="no datasets requested"):
        _run(tmp_path, "kr", date(2026, 10, 30), src, datasets=[])
    assert src.total_calls == 0


# --- empty first universe -------------------------------------------------------


@pytest.mark.unit
def test_empty_first_universe_venue_is_refused_and_writes_nothing(tmp_path):
    day = date(2026, 10, 30)
    src = FakeSources(kospi={})  # KOSPI master parses but holds no rows
    with pytest.raises(
        CollectError, match="KIS master KOSPI: .*no symbols; nothing written"
    ):
        _run(tmp_path, "kr", day, src, datasets=[DATASET_UNIVERSE])
    assert not partition_path(tmp_path, DATASET_UNIVERSE, "kr", day).exists()
    assert recorded_dates(tmp_path, DATASET_UNIVERSE, "kr") == []
    # the failure is a CollectError (exit 2), not a stray ValueError that would
    # abort the other datasets of the run
    assert not manifest_path(tmp_path, DATASET_UNIVERSE, "kr").exists()


@pytest.mark.unit
def test_empty_first_crypto_universe_is_refused(tmp_path):
    day = date(2026, 10, 30)
    with pytest.raises(CollectError, match="no USDT pairs|no symbols"):
        _run(
            tmp_path,
            "crypto",
            day,
            FakeSources(binance_pairs={}),
            datasets=[DATASET_UNIVERSE],
        )
    assert recorded_dates(tmp_path, DATASET_UNIVERSE, "crypto") == []


# --- listing diff reference when a universe record is missing --------------------


def _drop_universe_record(root, market, day):
    path = manifest_path(root, DATASET_UNIVERSE, market)
    keep = [
        line
        for line in path.read_text().splitlines(keepends=True)
        if f'"partition_date":"{day.isoformat()}"' not in line.replace(" ", "")
    ]
    path.write_text("".join(keep))
    part = partition_path(root, DATASET_UNIVERSE, market, day)
    for f in part.parent.iterdir():
        f.unlink()
    part.parent.rmdir()


@pytest.mark.unit
def test_missing_universe_record_falls_back_to_latest_universe_not_the_first(
    tmp_path,
):
    d1, d2, d3, d4 = (
        date(2026, 10, 29),
        date(2026, 10, 30),
        date(2026, 11, 2),
        date(2026, 11, 3),
    )
    base = {"005930": "SAMSUNG ELEC", "000660": "SK HYNIX"}
    added = {**base, "035420": "NAVER"}  # listed between d1 and d2
    both = [DATASET_UNIVERSE, DATASET_ACTIONS]
    _run(tmp_path, "kr", d1, FakeSources(kospi=base), both)
    _run(tmp_path, "kr", d2, FakeSources(kospi=added), both)
    d2_rows = read_partition(tmp_path, DATASET_ACTIONS, "kr", d2)
    assert [(r["instrument"], r["action_type"]) for r in d2_rows] == [
        ("035420", "listing_added")
    ]
    more = {**added, "068270": "CELLTRION"}  # listed between d2 and d3
    _run(tmp_path, "kr", d3, FakeSources(kospi=more), both)
    d3_rows = read_partition(tmp_path, DATASET_ACTIONS, "kr", d3)
    assert [(r["instrument"], r["action_type"]) for r in d3_rows] == [
        ("068270", "listing_added")
    ]
    # d3's universe record is lost while its actions record survived
    _drop_universe_record(tmp_path, "kr", d3)
    assert recorded_dates(tmp_path, DATASET_UNIVERSE, "kr") == [d1, d2]
    assert d3 in recorded_dates(tmp_path, DATASET_ACTIONS, "kr")

    _run(tmp_path, "kr", d4, FakeSources(kospi=more), both)

    d4_rows = read_partition(tmp_path, DATASET_ACTIONS, "kr", d4)
    # d1→d2 (recorded at d2) and d2→d3 (recorded at d3) must not come back
    # under a new compared_universe (a duplicate fact)
    assert [r for r in d4_rows if r["action_type"].startswith("listing")] == []
    rec = _actions_record(tmp_path, d4)
    assert rec["listing_compared"] == [d2.isoformat(), d4.isoformat()]
    assert rec["listing_reference_fallback"] == {
        "base": d3.isoformat(),
        "used": d2.isoformat(),
        "already_recorded_skipped": 1,
        "unobserved_interval": None,
    }


def _actions_record(root, day):
    import json

    for line in manifest_path(root, DATASET_ACTIONS, "kr").read_text().splitlines():
        rec = json.loads(line)
        if rec["partition_date"] == day.isoformat():
            return rec
    raise AssertionError(day)


@pytest.mark.unit
def test_no_universe_left_before_the_base_uses_the_nearest_after_it(tmp_path):
    """hk 2552 / #2255 review: with every universe on or before the last
    actions base lost, the reference is the nearest universe after the base,
    never the first one; nothing recorded is re-emitted and the unobservable
    interval is in the manifest."""
    d1, d2, d3, d4 = (
        date(2026, 10, 29),
        date(2026, 10, 30),
        date(2026, 11, 2),
        date(2026, 11, 3),
    )
    base = {"005930": "SAMSUNG ELEC", "000660": "SK HYNIX"}
    added = {**base, "035420": "NAVER"}  # listed between d1 and d2
    more = {**added, "068270": "CELLTRION"}  # listed between d3 and d4
    both = [DATASET_UNIVERSE, DATASET_ACTIONS]
    _run(tmp_path, "kr", d1, FakeSources(kospi=base), both)
    _run(tmp_path, "kr", d2, FakeSources(kospi=added), both)  # records 035420
    _run(tmp_path, "kr", d3, FakeSources(kospi=added), [DATASET_UNIVERSE])  # no actions
    # every universe on or before the base d2 is lost; d3 survives
    _drop_universe_record(tmp_path, "kr", d1)
    _drop_universe_record(tmp_path, "kr", d2)
    assert recorded_dates(tmp_path, DATASET_UNIVERSE, "kr") == [d3]
    assert recorded_dates(tmp_path, DATASET_ACTIONS, "kr") == [d1, d2]

    _run(tmp_path, "kr", d4, FakeSources(kospi=more), both)

    d4_rows = read_partition(tmp_path, DATASET_ACTIONS, "kr", d4)
    listings = [
        (r["instrument"], r["action_type"], r["compared_universe"])
        for r in d4_rows
        if r["action_type"].startswith("listing")
    ]
    # only the change after the reference; 035420 (recorded at d2) is not repeated
    assert listings == [("068270", "listing_added", [d3.isoformat(), d4.isoformat()])]
    rec = _actions_record(tmp_path, d4)
    assert rec["listing_reference_fallback"] == {
        "base": d2.isoformat(),
        "used": d3.isoformat(),
        "already_recorded_skipped": 0,
        "unobserved_interval": [d2.isoformat(), d3.isoformat()],
    }


@pytest.mark.unit
def test_empty_first_us_symbol_directory_is_refused(tmp_path):
    day = date(2026, 10, 30)
    src = FakeSources(empty_nasdaq_listed=True)
    with pytest.raises(
        CollectError, match="symbol directory nasdaqlisted: .*nothing written"
    ):
        _run(tmp_path, "us", day, src, datasets=[DATASET_UNIVERSE])
    assert recorded_dates(tmp_path, DATASET_UNIVERSE, "us") == []

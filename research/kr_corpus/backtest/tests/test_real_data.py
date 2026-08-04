from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from real_data import load_real_main_bars, load_real_main_stage_b_input


def _row(*, market: str, symbol: str, session: str) -> dict[str, object]:
    return {
        "session": session,
        "market": market,
        "ticker": symbol,
        "open": 100,
        "high": 101,
        "low": 99,
        "close": 100,
        "volume": 1_000,
        "value": None,
        "price_mode": "adjusted",
        "source_product": "synthetic_fixture",
    }


def _write_artifact(tmp_path: Path) -> tuple[Path, str]:
    artifact_root = tmp_path / "artifact-root"
    run_id = "main-fixture"
    root = artifact_root / "runs" / run_id
    root.mkdir(parents=True)
    sessions = ["2015-01-02", "2016-01-04"]
    preflight = root / "preflight.json"
    preflight.write_text(
        json.dumps({"session_calendar": "XKRX", "sessions": sessions}),
        encoding="utf-8",
    )

    for market in ("KOSPI", "KOSDAQ"):
        for symbol in ("000001", "000002"):
            for year, session in ((2015, sessions[0]), (2016, sessions[1])):
                path = root / "dataset" / f"market={market}" / f"year={year}"
                path = path / f"ticker={symbol}.parquet"
                path.parent.mkdir(parents=True, exist_ok=True)
                pq.write_table(
                    pa.Table.from_pylist(
                        [_row(market=market, symbol=symbol, session=session)]
                    ),
                    path,
                )

    checksum_paths = [preflight, *sorted((root / "dataset").rglob("*.parquet"))]
    (root / "checksums.sha256").write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
            f"{path.relative_to(root).as_posix()}\n"
            for path in checksum_paths
        ),
        encoding="utf-8",
    )
    return artifact_root, run_id


def test_symbol_selection_loads_all_yearly_partitions_and_records_coverage(
    tmp_path: Path,
) -> None:
    artifact_root, run_id = _write_artifact(tmp_path)

    loaded = load_real_main_stage_b_input(
        artifact_root=artifact_root,
        run_id=run_id,
        window_start=date(2015, 1, 1),
        window_end=date(2016, 12, 31),
        markets=("KOSPI", "KOSDAQ"),
        max_symbols=2,
    )

    assert len(loaded.bars) == 4
    assert loaded.market_sessions == {
        "KOSDAQ": (date(2015, 1, 2), date(2016, 1, 4)),
        "KOSPI": (date(2015, 1, 2), date(2016, 1, 4)),
    }
    for market in ("KOSPI", "KOSDAQ"):
        coverage = loaded.coverage["markets"][market]
        assert coverage["symbol_count"] == 1
        assert coverage["bar_count"] == 2
        assert coverage["year_count"] == 2
        assert coverage["years"] == [2015, 2016]
        assert coverage["symbols"] == {
            "000001": {
                "bar_count": 2,
                "year_count": 2,
                "years": [2015, 2016],
            }
        }
    assert loaded.coverage["session_reference"]["calendar"] == "XKRX"

    # The legacy list-returning loader remains a compatibility view of the
    # same verified input, rather than reintroducing one-partition selection.
    assert (
        len(
            load_real_main_bars(
                artifact_root=artifact_root,
                run_id=run_id,
                window_start=date(2015, 1, 1),
                window_end=date(2016, 12, 31),
                markets=("KOSPI", "KOSDAQ"),
                max_symbols=2,
            )
        )
        == 4
    )


def test_market_session_reference_requires_a_checksum(tmp_path: Path) -> None:
    artifact_root, run_id = _write_artifact(tmp_path)
    preflight = artifact_root / "runs" / run_id / "preflight.json"
    preflight.write_text(
        json.dumps({"session_calendar": "XKRX", "sessions": ["2015-01-02"]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="checksum mismatch: preflight.json"):
        load_real_main_stage_b_input(
            artifact_root=artifact_root,
            run_id=run_id,
            window_start=date(2015, 1, 1),
            window_end=date(2016, 12, 31),
            markets=("KOSPI",),
            max_symbols=1,
        )


def test_selected_symbol_checks_every_yearly_partition(tmp_path: Path) -> None:
    artifact_root, run_id = _write_artifact(tmp_path)
    second_year = (
        artifact_root
        / "runs"
        / run_id
        / "dataset/market=KOSPI/year=2016/ticker=000001.parquet"
    )
    second_year.write_bytes(b"tampered")

    with pytest.raises(
        ValueError,
        match="checksum mismatch: dataset/market=KOSPI/year=2016/ticker=000001.parquet",
    ):
        load_real_main_stage_b_input(
            artifact_root=artifact_root,
            run_id=run_id,
            window_start=date(2015, 1, 1),
            window_end=date(2016, 12, 31),
            markets=("KOSPI",),
            max_symbols=1,
        )

"""Manual exchange-info snapshot: idempotent per UTC day and append-only."""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from research.crypto_universe.public_http import FetchResponse
from research.crypto_universe.snapshot import (
    MANIFEST_NAME,
    SNAPSHOT_SOURCES,
    SnapshotError,
    latest_snapshot,
    take_snapshot,
)

BINANCE_URL = SNAPSHOT_SOURCES["binance_spot_exchange_info"]
UPBIT_URL = SNAPSHOT_SOURCES["upbit_market_all"]


class FakeFetch:
    """Serves a versioned payload per URL and counts requests."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.version = 1

    def __call__(self, url: str) -> FetchResponse:
        self.calls.append(url)
        if url == BINANCE_URL:
            payload = {
                "serverTime": self.version,
                "symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}],
            }
        elif url == UPBIT_URL:
            payload = [{"market": "KRW-BTC", "version": self.version}]
        else:
            raise AssertionError(f"unexpected URL {url}")
        return FetchResponse(200, json.dumps(payload).encode())


def _clock(day: int, hour: int = 1):
    return lambda: datetime(2026, 11, day, hour, 0, tzinfo=UTC)


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _manifest(root: Path) -> list[dict]:
    text = (root / "snapshots" / MANIFEST_NAME).read_text()
    return [json.loads(line) for line in text.splitlines()]


def test_second_run_same_day_fetches_nothing_and_writes_nothing(tmp_path: Path) -> None:
    fetch = FakeFetch()
    first = take_snapshot(tmp_path, fetch, now=_clock(1, 1))
    assert [o.action for o in first] == ["written", "written"]
    before = _tree(tmp_path)
    calls_before = len(fetch.calls)

    fetch.version = 2  # the exchange changed during the day
    second = take_snapshot(tmp_path, fetch, now=_clock(1, 23))

    assert [o.action for o in second] == ["already_present", "already_present"]
    assert len(fetch.calls) == calls_before  # no network on a same-day rerun
    assert _tree(tmp_path) == before  # byte-identical tree, nothing appended
    assert len(_manifest(tmp_path)) == 2


def test_next_day_appends_without_touching_earlier_snapshots(tmp_path: Path) -> None:
    fetch = FakeFetch()
    take_snapshot(tmp_path, fetch, now=_clock(1))
    day1 = _tree(tmp_path)
    manifest_day1 = (tmp_path / "snapshots" / MANIFEST_NAME).read_bytes()

    fetch.version = 2
    take_snapshot(tmp_path, fetch, now=_clock(2))
    day2 = _tree(tmp_path)

    for path, content in day1.items():
        if path.endswith(MANIFEST_NAME):
            continue
        assert day2[path] == content  # earlier snapshot files unchanged
    manifest_day2 = (tmp_path / "snapshots" / MANIFEST_NAME).read_bytes()
    assert manifest_day2.startswith(manifest_day1)  # append-only manifest
    entries = _manifest(tmp_path)
    assert [(e["snapshot_date"], e["source"]) for e in entries] == [
        ("2026-11-01", "binance_spot_exchange_info"),
        ("2026-11-01", "upbit_market_all"),
        ("2026-11-02", "binance_spot_exchange_info"),
        ("2026-11-02", "upbit_market_all"),
    ]
    for entry in entries:
        stored = (tmp_path / entry["path"]).read_bytes()
        assert hashlib.sha256(stored).hexdigest() == entry["stored_sha256"]
        assert hashlib.sha256(gzip.decompress(stored)).hexdigest() == entry["sha256"]

    snap_day, raw, digest = latest_snapshot(tmp_path, "binance_spot_exchange_info")
    assert snap_day.isoformat() == "2026-11-02"
    assert json.loads(raw)["serverTime"] == 2
    assert digest == entries[2]["sha256"]


def test_snapshot_files_are_never_overwritten(tmp_path: Path) -> None:
    take_snapshot(tmp_path, FakeFetch(), now=_clock(1))
    path = tmp_path / "snapshots" / "2026-11-01" / "upbit_market_all.json.gz"
    assert path.stat().st_mode & 0o222 == 0  # written read-only


def test_crash_between_file_and_manifest_converges(tmp_path: Path) -> None:
    fetch = FakeFetch()
    take_snapshot(tmp_path, fetch, now=_clock(1))
    manifest = tmp_path / "snapshots" / MANIFEST_NAME
    lines = manifest.read_text().splitlines(keepends=True)
    manifest.write_text(lines[0])  # simulate losing the second manifest append

    calls = len(fetch.calls)
    outcomes = take_snapshot(tmp_path, fetch, now=_clock(1, 5))
    assert [o.action for o in outcomes] == ["already_present", "manifest_repaired"]
    assert len(fetch.calls) == calls
    assert len(_manifest(tmp_path)) == 2
    third = take_snapshot(tmp_path, fetch, now=_clock(1, 6))
    assert [o.action for o in third] == ["already_present", "already_present"]
    assert len(_manifest(tmp_path)) == 2


def test_bad_response_writes_nothing(tmp_path: Path) -> None:
    def failing(url: str) -> FetchResponse:
        return FetchResponse(503, b"")

    with pytest.raises(SnapshotError):
        take_snapshot(tmp_path, failing, now=_clock(1))
    assert not list((tmp_path / "snapshots").glob("2026-11-01/*"))

    def not_json(url: str) -> FetchResponse:
        return FetchResponse(200, b"<html>")

    with pytest.raises(SnapshotError):
        take_snapshot(tmp_path, not_json, now=_clock(1))
    assert not list((tmp_path / "snapshots").glob("2026-11-01/*"))


def test_tampered_snapshot_is_refused(tmp_path: Path) -> None:
    take_snapshot(tmp_path, FakeFetch(), now=_clock(1))
    path = tmp_path / "snapshots" / "2026-11-01" / "upbit_market_all.json.gz"
    path.chmod(0o644)
    path.write_bytes(gzip.compress(b'[{"market":"KRW-FAKE"}]', mtime=0))
    with pytest.raises(SnapshotError, match="does not match"):
        latest_snapshot(tmp_path, "upbit_market_all")


def test_partial_final_file_is_never_recorded(tmp_path: Path) -> None:
    day_dir = tmp_path / "snapshots" / "2026-11-01"
    day_dir.mkdir(parents=True)
    for broken in (b"", gzip.compress(b'{"symbols": [1]}', mtime=0)[:12]):
        target = day_dir / "binance_spot_exchange_info.json.gz"
        target.write_bytes(broken)
        with pytest.raises(SnapshotError, match="gzip|not JSON"):
            take_snapshot(tmp_path, FakeFetch(), now=_clock(1))
        target.unlink()
    assert not (tmp_path / "snapshots" / MANIFEST_NAME).exists()
    # a complete but invalid payload is not recorded either
    (day_dir / "upbit_market_all.json.gz").write_bytes(gzip.compress(b"[]", mtime=0))
    with pytest.raises(SnapshotError, match="non-empty"):
        take_snapshot(
            tmp_path, FakeFetch(), now=_clock(1),
            sources={"upbit_market_all": UPBIT_URL},
        )  # fmt: skip


def test_publish_leaves_no_temp_files(tmp_path: Path) -> None:
    take_snapshot(tmp_path, FakeFetch(), now=_clock(1))
    names = sorted(p.name for p in (tmp_path / "snapshots" / "2026-11-01").iterdir())
    assert names == ["binance_spot_exchange_info.json.gz", "upbit_market_all.json.gz"]


def test_torn_manifest_line_is_closed_and_skipped(tmp_path: Path) -> None:
    fetch = FakeFetch()
    take_snapshot(tmp_path, fetch, now=_clock(1))
    manifest = tmp_path / "snapshots" / MANIFEST_NAME
    good = manifest.read_bytes()
    with manifest.open("ab") as handle:
        handle.write(b'{"snapshot_date":"2026-11-02","sou')  # crash mid-append
    take_snapshot(tmp_path, fetch, now=_clock(2))
    data = manifest.read_bytes()
    assert data.startswith(good)  # still append-only
    entries = [
        json.loads(line) for line in data.splitlines()
        if line.startswith(b"{") and line.endswith(b"}")
    ]  # fmt: skip
    assert len(entries) == 4
    day, _raw, _digest = latest_snapshot(tmp_path, "upbit_market_all")
    assert day.isoformat() == "2026-11-02"


def _rolling_clock(calls_before_midnight: int):
    """23:59:59 for the first N reads of the clock, 00:00:05 next day after."""
    count = {"n": 0}

    def clock() -> datetime:
        count["n"] += 1
        if count["n"] <= calls_before_midnight:
            return datetime(2026, 11, 1, 23, 59, 59, tzinfo=UTC)
        return datetime(2026, 11, 2, 0, 0, 5, tzinfo=UTC)

    return clock


@pytest.mark.parametrize("calls_before_midnight", [1, 2, 3])
def test_fetch_crossing_utc_midnight_writes_nothing(
    tmp_path: Path, calls_before_midnight: int
) -> None:
    # 1: rolls over during the first fetch; 2: between the two sources;
    # 3: during the second fetch. In every case no source may be published.
    with pytest.raises(SnapshotError, match="nothing was written"):
        take_snapshot(tmp_path, FakeFetch(), now=_rolling_clock(calls_before_midnight))
    assert not list((tmp_path / "snapshots").rglob("*.json.gz"))
    assert not (tmp_path / "snapshots" / MANIFEST_NAME).exists()
    # the rerun on the new day snapshots both sources
    outcomes = take_snapshot(tmp_path, FakeFetch(), now=_clock(2))
    assert [o.action for o in outcomes] == ["written", "written"]


def test_concurrent_runs_record_each_file_once(tmp_path: Path) -> None:
    import threading

    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            barrier.wait()
            take_snapshot(tmp_path, FakeFetch(), now=_clock(1))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(_manifest(tmp_path)) == 2
    latest_snapshot(tmp_path, "binance_spot_exchange_info")


def test_unrecorded_file_from_an_earlier_day_is_repaired_later(tmp_path: Path) -> None:
    fetch = FakeFetch()
    take_snapshot(tmp_path, fetch, now=_clock(1))
    manifest = tmp_path / "snapshots" / MANIFEST_NAME
    manifest.write_text(manifest.read_text().splitlines(keepends=True)[0])
    # the crash is only noticed two days later
    outcomes = take_snapshot(tmp_path, fetch, now=_clock(3))
    assert [(o.action, o.source, o.path.parent.name) for o in outcomes] == [
        ("manifest_repaired", "upbit_market_all", "2026-11-01"),
        ("written", "binance_spot_exchange_info", "2026-11-03"),
        ("written", "upbit_market_all", "2026-11-03"),
    ]
    dates = [(e["snapshot_date"], e["source"]) for e in _manifest(tmp_path)]
    assert ("2026-11-01", "upbit_market_all") in dates
    again = take_snapshot(tmp_path, fetch, now=_clock(3, 5))
    assert [o.action for o in again] == ["already_present", "already_present"]
    assert len(_manifest(tmp_path)) == 4

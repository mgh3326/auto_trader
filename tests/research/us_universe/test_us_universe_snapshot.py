"""Manual daily snapshot command: idempotent per day, append-only (network-free)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from research.us_universe.pit import sha256_hex
from research.us_universe.snapshot import _today_new_york, take_snapshot
from research.us_universe.store import AlreadyExistsError, read_jsonl, write_once
from research.us_universe.symdir import SOURCE_URLS, SymbolDirectoryError

from .us_universe_fixtures import nasdaq_body, other_body


class FakeFetch:
    def __init__(self, day: date, symbols: tuple[str, ...] = ("AAPL",)) -> None:
        self.calls: list[str] = []
        self.bodies = {
            SOURCE_URLS["nasdaqlisted"]: nasdaq_body(day, symbols).encode(),
            SOURCE_URLS["otherlisted"]: other_body(day, symbols).encode(),
        }

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        return self.bodies[url]


def tree(root: Path) -> dict[str, tuple[bytes, int]]:
    """Every file under root with its bytes and inode (catches replace-by-rename)."""
    return {
        str(p.relative_to(root)): (p.read_bytes(), p.stat().st_ino)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def manifest(root: Path) -> list[dict]:
    text = (root / "snapshots" / "manifest.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines()]


def test_second_run_same_day_fetches_and_writes_nothing(tmp_path: Path) -> None:
    day = date(2026, 11, 2)
    first = take_snapshot(
        tmp_path, fetch=FakeFetch(day), today=lambda: day, commit="c1"
    )
    assert first.action == "written"
    before = tree(tmp_path)

    refetch = FakeFetch(day, symbols=("CHANGED",))
    second = take_snapshot(tmp_path, fetch=refetch, today=lambda: day, commit="c2")

    assert second.action == "already_present"
    assert refetch.calls == []
    assert tree(tmp_path) == before
    assert [rec["day"] for rec in manifest(tmp_path)] == ["2026-11-02"]


def test_next_day_appends_without_touching_earlier_files(tmp_path: Path) -> None:
    d1, d2 = date(2026, 11, 2), date(2026, 11, 3)
    take_snapshot(tmp_path, fetch=FakeFetch(d1), today=lambda: d1, commit="c1")
    before = tree(tmp_path)
    manifest_before = (tmp_path / "snapshots" / "manifest.jsonl").read_bytes()

    take_snapshot(
        tmp_path, fetch=FakeFetch(d2, ("NEW",)), today=lambda: d2, commit="c1"
    )

    after = tree(tmp_path)
    for path, content in before.items():
        if path.endswith("manifest.jsonl"):
            continue
        assert after[path] == content  # same bytes, same inode
    manifest_after = (tmp_path / "snapshots" / "manifest.jsonl").read_bytes()
    assert manifest_after.startswith(manifest_before)  # append-only log
    records = manifest(tmp_path)
    assert [r["day"] for r in records] == ["2026-11-02", "2026-11-03"]
    assert records[1]["files"]["nasdaqlisted"]["file_created_at"] == "2026-11-03T17:03"
    assert (tmp_path / "snapshots" / "2026-11-03" / "otherlisted.txt").exists()


def test_invalid_body_writes_nothing(tmp_path: Path) -> None:
    day = date(2026, 11, 2)
    fetch = FakeFetch(day)
    fetch.bodies[SOURCE_URLS["otherlisted"]] = b"<html>maintenance</html>"
    with pytest.raises(SymbolDirectoryError):
        take_snapshot(tmp_path, fetch=fetch, today=lambda: day, commit="c1")
    assert tree(tmp_path) == {}


def test_crash_before_manifest_line_is_repaired_without_refetch(tmp_path: Path) -> None:
    day = date(2026, 11, 2)
    take_snapshot(tmp_path, fetch=FakeFetch(day), today=lambda: day, commit="c1")
    log = tmp_path / "snapshots" / "manifest.jsonl"
    original = log.read_text()
    log.write_text("")  # simulate a crash between publish and log append
    fetch = FakeFetch(day)

    result = take_snapshot(tmp_path, fetch=fetch, today=lambda: day, commit="c1")

    assert result.action == "manifest_repaired"
    assert fetch.calls == []
    assert log.read_text() == original
    assert take_snapshot(tmp_path, fetch=fetch, today=lambda: day).action == (
        "already_present"
    )


def test_write_once_refuses_to_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    write_once(target, b"first")
    with pytest.raises(AlreadyExistsError):
        write_once(target, b"second")
    assert target.read_bytes() == b"first"
    assert [p.name for p in tmp_path.iterdir()] == ["a.txt"]  # no stray .partial


def test_crash_with_one_file_published_fetches_only_the_missing_one(
    tmp_path: Path,
) -> None:
    day = date(2026, 11, 2)
    pre = tmp_path / "snapshots" / "2026-11-02" / "nasdaqlisted.txt"
    write_once(pre, nasdaq_body(day, ("FIRST",)).encode())
    before = (pre.read_bytes(), pre.stat().st_ino)
    fetch = FakeFetch(day, ("SECOND",))

    result = take_snapshot(tmp_path, fetch=fetch, today=lambda: day, commit="c1")

    assert result.action == "written"
    assert fetch.calls == [SOURCE_URLS["otherlisted"]]
    assert (pre.read_bytes(), pre.stat().st_ino) == before
    (record,) = manifest(tmp_path)
    assert record["files"]["nasdaqlisted"]["sha256"] == sha256_hex(before[0])


def test_torn_manifest_line_does_not_block_later_days(tmp_path: Path) -> None:
    d1, d2 = date(2026, 11, 2), date(2026, 11, 3)
    take_snapshot(tmp_path, fetch=FakeFetch(d1), today=lambda: d1, commit="c1")
    log = tmp_path / "snapshots" / "manifest.jsonl"
    with open(log, "ab") as fh:
        fh.write(b'{"day":"2026-11-0')  # crash mid-append
    torn = log.read_bytes()

    take_snapshot(tmp_path, fetch=FakeFetch(d2), today=lambda: d2, commit="c1")

    assert log.read_bytes().startswith(torn)  # nothing rewritten
    assert [r["day"] for r in read_jsonl(log)] == ["2026-11-02", "2026-11-03"]


def test_day_key_is_the_new_york_date() -> None:
    # 2026-11-02 03:30 UTC is still 2026-11-01 in New York
    now = datetime(2026, 11, 2, 3, 30, tzinfo=UTC)
    assert _today_new_york(now) == date(2026, 11, 1)

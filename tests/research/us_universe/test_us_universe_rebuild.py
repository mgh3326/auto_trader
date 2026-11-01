"""Rebuild from cached public archive captures (network-free)."""

from __future__ import annotations

import ast
import gzip
import json
from datetime import date
from pathlib import Path

import pytest

import research.us_universe as pkg
from research.us_universe.pit import rows_from_csv, sha256_hex, universe_as_of
from research.us_universe.rebuild import (
    OUTPUT_BUILD_INFO,
    OUTPUT_COVERAGE,
    OUTPUT_CSV,
    OUTPUT_INPUTS,
    OUTPUT_MANIFEST,
    StaleSeriesError,
    capture_url,
    cdx_url,
    quarantine_path,
    rebuild,
    refresh_wayback_cache,
    wayback_cache_path,
)
from research.us_universe.rebuild import main as rebuild_main
from research.us_universe.snapshot import take_snapshot
from research.us_universe.store import AlreadyExistsError

from .us_universe_fixtures import nasdaq_body, other_body

D2010, D2018, D2026 = date(2010, 5, 3), date(2018, 7, 2), date(2026, 10, 30)


def seed(root: Path) -> None:
    cache = {
        ("nasdaqlisted", "20100504000000"): nasdaq_body(
            D2010, ("GONE", "AAPL"), old=True
        ),
        ("nasdaqlisted", "20180703000000"): nasdaq_body(D2018, ("AAPL", "MID")),
        ("otherlisted", "20100504000000"): other_body(D2010, ("XOM",), old=True),
        # captures stored gzip-encoded by the archive must still parse
        ("otherlisted", "20180703000000"): gzip.compress(
            other_body(D2018, ("XOM", "SPIN")).encode()
        ),
        # an archived error page: recorded as a rejected input, not a delisting
        ("otherlisted", "20190101000000"): b"<html>502 Bad Gateway</html>",
    }
    for (series, ts), body in cache.items():
        path = wayback_cache_path(root, series, ts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body if isinstance(body, bytes) else body.encode())

    class Fetch:
        def __call__(self, url: str) -> bytes:
            if "nasdaqlisted" in url:
                return nasdaq_body(D2026, ("AAPL", "NEWCO")).encode()
            return other_body(D2026, ("XOM",)).encode()

    take_snapshot(root, fetch=Fetch(), today=lambda: date(2026, 11, 1), commit="c")


def test_offline_rebuild_is_byte_reproducible(tmp_path: Path) -> None:
    seed(tmp_path)
    a = rebuild(tmp_path, tmp_path / "out_a", commit="abc")
    # a different generating commit only changes the build_info sidecar
    b = rebuild(tmp_path, tmp_path / "out_b", commit="def-dirty")
    assert a == b
    for name in (OUTPUT_CSV, OUTPUT_INPUTS, OUTPUT_MANIFEST, OUTPUT_COVERAGE):
        assert (tmp_path / "out_a" / name).read_bytes() == (
            tmp_path / "out_b" / name
        ).read_bytes()
    csv_bytes = (tmp_path / "out_a" / OUTPUT_CSV).read_bytes()
    assert a["outputs"][OUTPUT_CSV]["sha256"] == sha256_hex(csv_bytes)
    info = json.loads((tmp_path / "out_b" / OUTPUT_BUILD_INFO).read_text())
    assert info == {
        "code_commit": "def-dirty",
        "manifest_sha256": sha256_hex(
            (tmp_path / "out_a" / OUTPUT_MANIFEST).read_bytes()
        ),
    }


def test_manifest_records_inputs_coverage_and_inactive_share(tmp_path: Path) -> None:
    seed(tmp_path)
    m = rebuild(tmp_path, tmp_path / "out", commit="abc")
    assert m["inputs"]["accepted"] == 6 and m["inputs"]["rejected"] == 1
    inputs_bytes = (tmp_path / "out" / OUTPUT_INPUTS).read_bytes()
    files = [json.loads(line) for line in inputs_bytes.splitlines()]
    assert len(files) == 7 and all(len(f["sha256"]) == 64 for f in files)
    assert m["inputs"][OUTPUT_INPUTS]["sha256"] == sha256_hex(inputs_bytes)
    (rejected,) = m["inputs"]["rejected_files"]
    assert rejected["ref"] == "20190101000000"
    assert rejected in files
    assert m["outputs"][OUTPUT_CSV]["rows"] == len(
        rows_from_csv((tmp_path / "out" / OUTPUT_CSV).read_bytes())
    )
    assert "--offline" in m["generation"]["command"]
    assert m["series_coverage"]["nasdaqlisted"]["observation_days"] == 3
    assert m["as_of_latest_observation"] == "2026-10-30"
    shares = {s["scope"]: s for s in m["inactive_share"]}
    # GONE, MID and SPIN are inactive; the error page delisted nothing.
    total = shares["all_symbols_all_time"]
    assert total["inactive"] == 3
    assert m["current_corpus_reference"]["inactive_share"] == 0.006
    assert json.loads((tmp_path / "out" / OUTPUT_MANIFEST).read_text()) == m


def test_rebuilt_table_answers_point_in_time_queries(tmp_path: Path) -> None:
    seed(tmp_path)
    rebuild(tmp_path, tmp_path / "out", commit="abc")
    rows = rows_from_csv((tmp_path / "out" / OUTPUT_CSV).read_bytes())
    at_2015 = universe_as_of(rows, date(2015, 1, 1))
    # GONE: seen 2010-05-03, first absent 2018-07-02 — kept until then
    assert "GONE" in universe_as_of(rows, D2010) and "GONE" in at_2015
    assert "GONE" not in universe_as_of(rows, D2018)
    assert "MID" not in at_2015 and "MID" in universe_as_of(rows, D2018)
    assert "NEWCO" not in universe_as_of(rows, D2018)
    assert "NEWCO" in universe_as_of(rows, D2026)


def test_corrupt_gzip_capture_is_a_rejected_input(tmp_path: Path) -> None:
    seed(tmp_path)
    bad = bytearray(gzip.compress(nasdaq_body(D2018, ("AAPL",)).encode()))
    bad[20:30] = b"\xff" * 10
    path = wayback_cache_path(tmp_path, "nasdaqlisted", "20180801000000")
    path.write_bytes(bytes(bad))
    m = rebuild(tmp_path, tmp_path / "out", commit="abc")
    (rej,) = m["inputs"]["rejected_files"][:1]
    assert rej["ref"] == "20180801000000" and rej["accepted"] is False


def test_stale_series_refuses_to_build(tmp_path: Path) -> None:
    # otherlisted stops in 2018 while nasdaqlisted reaches 2026: every NYSE
    # name delisted since would read as active, so the build must refuse.
    seed(tmp_path)
    for p in (tmp_path / "snapshots").glob("*/otherlisted.txt"):
        p.unlink()
    with pytest.raises(StaleSeriesError, match="otherlisted"):
        rebuild(tmp_path, tmp_path / "out", commit="abc")
    assert not (tmp_path / "out").exists()


def test_rebuild_refuses_to_overwrite_outputs(tmp_path: Path) -> None:
    seed(tmp_path)
    rebuild(tmp_path, tmp_path / "out", commit="abc")
    with pytest.raises(AlreadyExistsError):
        rebuild(tmp_path, tmp_path / "out", commit="abc")


def test_refresh_fetches_only_missing_captures(tmp_path: Path) -> None:
    calls: list[str] = []
    cdx = json.dumps(
        [
            ["timestamp", "original", "statuscode", "digest"],
            ["20100504000000", "http://www.nasdaqtrader.com/x.txt", "200", "A"],
            ["20180703000000", "http://www.nasdaqtrader.com/x.txt", "200", "B"],
        ]
    ).encode()
    wayback_cache_path(tmp_path, "nasdaqlisted", "20100504000000").parent.mkdir(
        parents=True
    )
    wayback_cache_path(tmp_path, "nasdaqlisted", "20100504000000").write_bytes(b"x")

    def fetch(url: str) -> bytes:
        calls.append(url)
        if url == cdx_url("nasdaqlisted"):
            return cdx
        if url == cdx_url("otherlisted"):
            raise TimeoutError
        return VALID_2018

    failures = refresh_wayback_cache(tmp_path, fetch)

    assert calls == [
        cdx_url("nasdaqlisted"),
        capture_url("20180703000000", "http://www.nasdaqtrader.com/x.txt"),
        cdx_url("otherlisted"),
    ]
    assert failures == ["otherlisted: CDX listing failed: TimeoutError"]
    # an existing entry is not refetched, even an invalid one, by default
    assert (
        wayback_cache_path(tmp_path, "nasdaqlisted", "20100504000000").read_bytes()
        == b"x"
    )
    assert (
        wayback_cache_path(tmp_path, "nasdaqlisted", "20180703000000").read_bytes()
        == VALID_2018
    )


VALID_2018 = nasdaq_body(D2018, ("AAPL",)).encode()
ORIGINAL = "http://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"


def _one_capture_cdx(ts: str) -> bytes:
    return json.dumps(
        [["timestamp", "original", "statuscode", "digest"], [ts, ORIGINAL, "200", "A"]]
    ).encode()


def _fetcher(capture_body: bytes, calls: list[str]):
    def fetch(url: str) -> bytes:
        calls.append(url)
        if url == cdx_url("nasdaqlisted"):
            return _one_capture_cdx("20180703000000")
        if url == cdx_url("otherlisted"):
            return json.dumps([["timestamp", "original"]]).encode()
        return capture_body

    return fetch


@pytest.mark.parametrize(
    "bad_body",
    [
        b"<html>502 Bad Gateway</html>",
        VALID_2018[: len(VALID_2018) // 2],  # truncated: no footer
        gzip.compress(VALID_2018)[:-40],  # truncated gzip stream
    ],
)
def test_invalid_capture_is_not_cached_and_is_retried(
    tmp_path: Path, bad_body: bytes
) -> None:
    path = wayback_cache_path(tmp_path, "nasdaqlisted", "20180703000000")
    failures = refresh_wayback_cache(tmp_path, _fetcher(bad_body, []))
    assert not path.exists()
    assert len(failures) == 1 and "rejected body, not cached" in failures[0]

    calls: list[str] = []
    assert refresh_wayback_cache(tmp_path, _fetcher(VALID_2018, calls)) == []
    assert path.read_bytes() == VALID_2018  # the slot was free for the retry


def test_refresh_rejected_replaces_only_invalid_entries_and_keeps_old_bytes(
    tmp_path: Path,
) -> None:
    path = wayback_cache_path(tmp_path, "nasdaqlisted", "20180703000000")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"<html>left by older code</html>")
    rejected_dir = tmp_path / "sources" / "wayback-rejected" / "nasdaqlisted"

    # a refetch that is still invalid leaves the cached file untouched
    failures = refresh_wayback_cache(
        tmp_path, _fetcher(b"<html>still down</html>", []), refresh_rejected=True
    )
    assert len(failures) == 1
    assert path.read_bytes() == b"<html>left by older code</html>"
    assert not rejected_dir.exists()

    # a valid refetch moves the old bytes aside, then publishes the new body
    assert (
        refresh_wayback_cache(tmp_path, _fetcher(VALID_2018, []), refresh_rejected=True)
        == []
    )
    assert path.read_bytes() == VALID_2018
    (kept,) = rejected_dir.iterdir()
    assert kept.read_bytes() == b"<html>left by older code</html>"
    assert kept.name.startswith("20180703000000.")

    # a valid cached capture is never refetched or replaced
    calls: list[str] = []
    refresh_wayback_cache(tmp_path, _fetcher(b"other", calls), refresh_rejected=True)
    assert capture_url("20180703000000", ORIGINAL) not in calls
    assert path.read_bytes() == VALID_2018


def test_refresh_rejected_cannot_be_combined_with_offline(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        rebuild_main(
            [
                "--root",
                str(tmp_path),
                "--out",
                str(tmp_path / "o"),
                "--offline",
                "--refresh-rejected",
            ]
        )


def test_package_reaches_no_broker_database_or_order_code() -> None:
    allowed_app = {"app.core.symbol"}
    for path in Path(pkg.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            for name in names:
                if name.startswith("app"):
                    assert name in allowed_app, f"{path.name} imports {name}"
                assert not name.startswith(("sqlalchemy", "asyncpg", "httpx")), (
                    f"{path.name} imports {name}"
                )


def test_refresh_rejected_resumes_after_an_interrupted_quarantine(
    tmp_path: Path,
) -> None:
    bad = b"<html>left by older code</html>"
    path = wayback_cache_path(tmp_path, "nasdaqlisted", "20180703000000")
    path.parent.mkdir(parents=True)
    path.write_bytes(bad)
    # crash state: the old bytes were already moved aside, the unlink was not
    kept = quarantine_path(tmp_path, "nasdaqlisted", "20180703000000", bad)
    kept.parent.mkdir(parents=True)
    kept.write_bytes(bad)

    failures = refresh_wayback_cache(
        tmp_path, _fetcher(VALID_2018, []), refresh_rejected=True
    )

    assert failures == []
    assert path.read_bytes() == VALID_2018
    assert kept.read_bytes() == bad


def test_capture_published_by_a_concurrent_run_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import research.us_universe.rebuild as rebuild_module

    path = wayback_cache_path(tmp_path, "nasdaqlisted", "20180703000000")
    real_write_once = rebuild_module.write_once

    def racing_write_once(target: Path, data: bytes) -> None:
        if target == path:  # another process wins the race for this slot
            real_write_once(target, VALID_2018)
        real_write_once(target, data)

    monkeypatch.setattr(rebuild_module, "write_once", racing_write_once)
    assert refresh_wayback_cache(tmp_path, _fetcher(VALID_2018, [])) == []
    assert path.read_bytes() == VALID_2018

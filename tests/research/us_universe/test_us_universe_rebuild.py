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
    OUTPUT_COVERAGE,
    OUTPUT_CSV,
    OUTPUT_MANIFEST,
    capture_url,
    cdx_url,
    rebuild,
    refresh_wayback_cache,
    wayback_cache_path,
)
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
    b = rebuild(tmp_path, tmp_path / "out_b", commit="abc")
    assert a == b
    for name in (OUTPUT_CSV, OUTPUT_MANIFEST, OUTPUT_COVERAGE):
        assert (tmp_path / "out_a" / name).read_bytes() == (
            tmp_path / "out_b" / name
        ).read_bytes()
    csv_bytes = (tmp_path / "out_a" / OUTPUT_CSV).read_bytes()
    assert a["outputs"][OUTPUT_CSV]["sha256"] == sha256_hex(csv_bytes)


def test_manifest_records_inputs_coverage_and_inactive_share(tmp_path: Path) -> None:
    seed(tmp_path)
    m = rebuild(tmp_path, tmp_path / "out", commit="abc")
    assert m["inputs"]["accepted"] == 6 and m["inputs"]["rejected"] == 1
    (rejected,) = [f for f in m["inputs"]["files"] if not f["accepted"]]
    assert rejected["ref"] == "20190101000000"
    assert all(len(f["sha256"]) == 64 for f in m["inputs"]["files"])
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
    assert "GONE" in universe_as_of(rows, D2010)
    assert "GONE" not in at_2015
    assert "MID" not in at_2015 and "MID" in universe_as_of(rows, D2018)
    assert "NEWCO" not in universe_as_of(rows, D2018)
    assert "NEWCO" in universe_as_of(rows, D2026)


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
        return b"body"

    failures = refresh_wayback_cache(tmp_path, fetch)

    assert calls == [
        cdx_url("nasdaqlisted"),
        capture_url("20180703000000", "http://www.nasdaqtrader.com/x.txt"),
        cdx_url("otherlisted"),
    ]
    assert failures == ["otherlisted: CDX listing failed: TimeoutError"]
    assert (
        wayback_cache_path(tmp_path, "nasdaqlisted", "20100504000000").read_bytes()
        == b"x"
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

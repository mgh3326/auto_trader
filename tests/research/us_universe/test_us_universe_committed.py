"""The committed rebuild in research/us_universe/data_manifests is consistent."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import research.us_universe as pkg
from research.us_universe.pit import (
    inactive_shares,
    rows_from_csv,
    rows_to_csv,
    sha256_hex,
    universe_as_of,
)
from research.us_universe.rebuild import (
    CORPUS_WINDOW_START,
    OUTPUT_BUILD_INFO,
    OUTPUT_CSV,
    OUTPUT_MANIFEST,
)

DATA = Path(pkg.__file__).parent / "data_manifests"
CSV_BYTES = (DATA / OUTPUT_CSV).read_bytes()
MANIFEST_BYTES = (DATA / OUTPUT_MANIFEST).read_bytes()
MANIFEST = json.loads(MANIFEST_BYTES)
ROWS = rows_from_csv(CSV_BYTES)


def test_digests_chain_csv_to_manifest_to_build_info() -> None:
    assert MANIFEST["outputs"][OUTPUT_CSV]["sha256"] == sha256_hex(CSV_BYTES)
    info = json.loads((DATA / OUTPUT_BUILD_INFO).read_text())
    assert info["manifest_sha256"] == sha256_hex(MANIFEST_BYTES)
    assert not info["code_commit"].endswith("-dirty")
    assert rows_to_csv(ROWS) == CSV_BYTES  # canonical serialisation


def test_reported_inactive_share_matches_the_table() -> None:
    latest = date.fromisoformat(MANIFEST["as_of_latest_observation"])
    recomputed = [
        s.as_dict()
        for s in inactive_shares(
            ROWS, window_start=CORPUS_WINDOW_START, window_end=latest
        )
    ]
    assert recomputed == MANIFEST["inactive_share"]
    assert MANIFEST["inputs"]["rejected"] == 0


def test_known_listing_events() -> None:
    # SVB Financial (failed 2023-03) and Activision (acquired 2023-10)
    assert "SIVB" in universe_as_of(ROWS, date(2020, 1, 2))
    assert "SIVB" not in universe_as_of(ROWS, date(2024, 1, 2))
    assert "ATVI" in universe_as_of(ROWS, date(2023, 1, 3))
    assert "ATVI" not in universe_as_of(ROWS, date(2025, 1, 2))
    # Rivian (IPO 2021-11) is not in the universe before its first observation
    rivn = [r for r in ROWS if r.symbol == "RIVN"]
    assert rivn and all(r.listed_from > date(2021, 11, 1) for r in rivn)
    assert "RIVN" not in universe_as_of(ROWS, date(2020, 1, 2))
    assert "AAPL" in universe_as_of(ROWS, date(2008, 6, 2))

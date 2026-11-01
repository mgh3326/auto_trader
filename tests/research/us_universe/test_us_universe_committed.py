"""The committed rebuild record in research/us_universe/data_manifests.

The PIT CSV itself lives outside the repo (see README); the committed files
pin it by sha256 and row count and name the command that regenerates it.
"""

from __future__ import annotations

import json
from pathlib import Path

import research.us_universe as pkg
from research.us_universe.pit import sha256_hex
from research.us_universe.rebuild import (
    GENERATION_COMMAND,
    OUTPUT_BUILD_INFO,
    OUTPUT_CSV,
    OUTPUT_INPUTS,
    OUTPUT_MANIFEST,
)

DATA = Path(pkg.__file__).parent / "data_manifests"
MANIFEST_BYTES = (DATA / OUTPUT_MANIFEST).read_bytes()
MANIFEST = json.loads(MANIFEST_BYTES)


def test_digests_chain_inputs_to_manifest_to_build_info() -> None:
    inputs_bytes = (DATA / OUTPUT_INPUTS).read_bytes()
    assert MANIFEST["inputs"][OUTPUT_INPUTS]["sha256"] == sha256_hex(inputs_bytes)
    info = json.loads((DATA / OUTPUT_BUILD_INFO).read_text())
    assert info["manifest_sha256"] == sha256_hex(MANIFEST_BYTES)
    assert not info["code_commit"].endswith("-dirty")


def test_manifest_pins_the_out_of_repo_table() -> None:
    csv = MANIFEST["outputs"][OUTPUT_CSV]
    assert len(csv["sha256"]) == 64 and csv["rows"] > 0 and csv["symbols"] > 0
    assert MANIFEST["generation"]["command"] == GENERATION_COMMAND


def test_inputs_summary_matches_the_inputs_file() -> None:
    files = [json.loads(x) for x in (DATA / OUTPUT_INPUTS).read_bytes().splitlines()]
    accepted = sum(1 for f in files if f["accepted"])
    assert (MANIFEST["inputs"]["accepted"], MANIFEST["inputs"]["rejected"]) == (
        accepted,
        len(files) - accepted,
    )
    assert MANIFEST["inputs"]["rejected_files"] == [
        f for f in files if not f["accepted"]
    ]
    scopes = {s["scope"] for s in MANIFEST["inactive_share"]}
    assert "likely_common_stock_listed_in_2016-01-01..2026-10-30" in scopes

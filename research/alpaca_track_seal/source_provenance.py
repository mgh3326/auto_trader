"""ROB-1060 H2 — sealed-source-data integrity gate.

Every raw JSON fixture under ``sealed_source_data/`` is a verbatim copy of an
external authority file recorded in the ROB-1060 issue / ROB-1058 epic
authority table (SHA-256-pinned). This module re-verifies the copy's SHA-256
against the pinned literal on every load — the same "trust but verify"
discipline ``research/nautilus_scalping/rob941_manifest.py``'s archive
checksum re-verification and ``StrategySourceProvenance.verified_source_sha256``
use — so a silently-edited or truncated fixture fails closed instead of
sealing garbage.

Pure stdlib only. No app/DB/network import (this module is part of the
research-side data layer, not the CLI registration boundary).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

__all__ = [
    "BASIS_ANALYSIS_FULL_SHA256",
    "FEE_PROBE_SHA256",
    "PARAMS_SEAL_DRAFT_DOC_SHA256",
    "PREREGISTRATION_DOC_SHA256",
    "SPREAD_CENSUS_SHA256",
    "UNIVERSE_MAP_SHA256",
    "SourceIntegrityError",
    "load_basis_analysis_full",
    "load_fee_probe",
    "load_spread_census",
    "load_universe_map",
    "load_verified_json",
]

_SEALED_DIR = Path(__file__).resolve().parent / "sealed_source_data"

# Pinned literally from the ROB-1060 issue body / ROB-1058 epic authority
# table (2026-07-25). These strings are the immutable ground truth this
# module verifies loaded bytes against — never derived from the files
# themselves (that would make the check vacuous).
UNIVERSE_MAP_SHA256 = "512285ebf67bb49dc1844d7c76dda4ea09dc19cbfb5968d32caee4a688cae8b2"
SPREAD_CENSUS_SHA256 = "10d5a1c52c77d6c2a1ce81adb4776fec69aefdcc2dbc7e87f08672b185113609"
BASIS_ANALYSIS_FULL_SHA256 = (
    "835e2abea219d3e78eec21f7ef64d939d7945ca764e3684136f41287e9b0378c"
)
FEE_PROBE_SHA256 = "b94532dcd3c2cc8aa04a137c6471ff3ffa6d2ba4dffca3af3c287ca7b1532a5d"

# The two markdown authority documents are NOT shipped as files in this repo
# (they live in the separate herdr-strategy-prompts workspace) -- only their
# pinned SHA-256 is recorded, as literal provenance for the seal artifact.
PREREGISTRATION_DOC_SHA256 = (
    "67b5d3c2255dd7c8b7dbc8aa8cbb44e467dc1e104d852e28edb36b818a84d349"
)
PARAMS_SEAL_DRAFT_DOC_SHA256 = (
    "dc9232ef73dfca733a77bc89ec7cbb825f0a692e29707915372ae39b6b0fb140"
)


class SourceIntegrityError(Exception):
    """A sealed-source-data fixture's bytes do not match its pinned SHA-256."""


def load_verified_json(path: str | Path, *, expected_sha256: str) -> Any:
    """Read+parse ``path`` as JSON, but ONLY after verifying its raw bytes
    hash to ``expected_sha256``. Fails closed (never returns partially, never
    falls back to an unverified read) on any mismatch."""
    path = Path(path)
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise SourceIntegrityError(
            f"{path}: sha256 mismatch (expected {expected_sha256}, actual {actual}) "
            "-- refusing to seal from tampered/stale source data"
        )
    return json.loads(raw.decode("utf-8"))


def load_universe_map() -> Any:
    return load_verified_json(
        _SEALED_DIR / "universe_map_2026-07-25.json",
        expected_sha256=UNIVERSE_MAP_SHA256,
    )


def load_spread_census() -> Any:
    return load_verified_json(
        _SEALED_DIR / "spread_census_2026-07-25.json",
        expected_sha256=SPREAD_CENSUS_SHA256,
    )


def load_basis_analysis_full() -> Any:
    return load_verified_json(
        _SEALED_DIR / "basis_analysis_full.json",
        expected_sha256=BASIS_ANALYSIS_FULL_SHA256,
    )


def load_fee_probe() -> Any:
    return load_verified_json(
        _SEALED_DIR / "fee_probe_20260725T142435Z.json",
        expected_sha256=FEE_PROBE_SHA256,
    )

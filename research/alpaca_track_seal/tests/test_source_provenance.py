"""ROB-1060 H2 — RED-first: the sealed-source-data integrity gate.

Every raw JSON fixture this package ships under ``sealed_source_data/`` is a
verbatim copy of an external authority file (from
``herdr-strategy-prompts/alpaca-basis-data/``). The seal is only trustworthy
if the shipped bytes are proven, at import/load time, to hash to the EXACT
SHA-256 pinned in the ROB-1060 Linear issue / ROB-1058 epic authority table —
not merely asserted to. A silently-edited or truncated fixture must fail
closed, not seal garbage.
"""

from __future__ import annotations

import hashlib

import pytest


def test_load_verified_json_rejects_tampered_bytes(tmp_path):
    import source_provenance as sp

    p = tmp_path / "fixture.json"
    p.write_text('{"a": 1}')
    wrong_hash = "0" * 64
    with pytest.raises(sp.SourceIntegrityError, match="sha256 mismatch"):
        sp.load_verified_json(p, expected_sha256=wrong_hash)


def test_load_verified_json_accepts_matching_bytes(tmp_path):
    import source_provenance as sp

    p = tmp_path / "fixture.json"
    p.write_bytes(b'{"a": 1}')
    actual = hashlib.sha256(p.read_bytes()).hexdigest()
    data = sp.load_verified_json(p, expected_sha256=actual)
    assert data == {"a": 1}


def test_all_four_shipped_fixtures_match_the_pinned_authority_sha256():
    import source_provenance as sp

    # Every one of the four raw data files this package ships must verify
    # against the literal hash recorded in the Linear issue / epic authority
    # table — the whole point of shipping a "sealed_source_data" copy.
    sp.load_universe_map()
    sp.load_spread_census()
    sp.load_basis_analysis_full()
    sp.load_fee_probe()


def test_pinned_hash_constants_match_the_linear_issue_literally():
    import source_provenance as sp

    assert (
        sp.UNIVERSE_MAP_SHA256
        == "512285ebf67bb49dc1844d7c76dda4ea09dc19cbfb5968d32caee4a688cae8b2"
    )
    assert (
        sp.SPREAD_CENSUS_SHA256
        == "10d5a1c52c77d6c2a1ce81adb4776fec69aefdcc2dbc7e87f08672b185113609"
    )
    assert (
        sp.BASIS_ANALYSIS_FULL_SHA256
        == "835e2abea219d3e78eec21f7ef64d939d7945ca764e3684136f41287e9b0378c"
    )
    assert (
        sp.FEE_PROBE_SHA256
        == "b94532dcd3c2cc8aa04a137c6471ff3ffa6d2ba4dffca3af3c287ca7b1532a5d"
    )
    assert (
        sp.PREREGISTRATION_DOC_SHA256
        == "67b5d3c2255dd7c8b7dbc8aa8cbb44e467dc1e104d852e28edb36b818a84d349"
    )
    assert (
        sp.PARAMS_SEAL_DRAFT_DOC_SHA256
        == "dc9232ef73dfca733a77bc89ec7cbb825f0a692e29707915372ae39b6b0fb140"
    )

"""ROB-1060 H2 — RED-first: the top-level sealed artifact combining the
16-config domain + the 4 execution params into ONE immutable, semantically
hashed record. H3-H6 read ONLY this artifact (AC18) — never redefine a
hardcoded copy of any value in it.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys

import pytest


def test_build_sealed_artifact_has_16_configs_and_full_params():
    import artifact as m

    a = m.build_sealed_artifact()
    assert len(a.configs) == 16
    assert a.params.run_status.total_configs == 16


def test_semantic_hash_is_64_hex():
    import artifact as m

    a = m.build_sealed_artifact()
    h = a.semantic_hash()
    assert len(h) == 64
    int(h, 16)


def test_semantic_hash_reproducible_within_one_process():
    import artifact as m

    a1 = m.build_sealed_artifact()
    a2 = m.build_sealed_artifact()
    assert a1.semantic_hash() == a2.semantic_hash()


def test_semantic_hash_reproducible_across_separate_process_invocations():
    """Genuinely separate process invocations (not just two objects in one
    process) must derive the identical semantic hash — proves the artifact
    carries no hidden per-process state (no id()/hash-seed/wall-clock
    leakage)."""
    package_root = __file__.rsplit("/tests/", 1)[0]  # .../research/alpaca_track_seal
    research_root = package_root.rsplit("/", 1)[0]  # .../research
    repo_root = research_root.rsplit("/", 1)[0]  # .../auto_trader.<worktree>
    nautilus_scalping = f"{research_root}/nautilus_scalping"
    script = (
        "import sys; "
        f"sys.path.insert(0, {package_root!r}); "
        f"sys.path.insert(0, {nautilus_scalping!r}); "
        f"sys.path.insert(0, {repo_root!r}); "
        "import artifact as m; print(m.build_sealed_artifact().semantic_hash())"
    )
    outputs = []
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"},
        )
        outputs.append(result.stdout.strip())
    assert len(outputs[0]) == 64
    assert outputs[0] == outputs[1]


def test_configs_are_keyed_by_config_id_so_top_level_order_cannot_affect_the_hash():
    import artifact as m

    a = m.build_sealed_artifact()
    d = a.to_dict()
    assert isinstance(d["configs"], dict)
    assert set(d["configs"]) == {c.config_id for c in a.configs}


def test_artifact_is_frozen_dataclass_no_attribute_mutation_possible():
    import artifact as m

    a = m.build_sealed_artifact()
    with pytest.raises(dataclasses.FrozenInstanceError, match="cannot assign to field"):
        a.configs = ()  # type: ignore[misc]


def test_save_then_load_round_trips_to_the_identical_semantic_hash(tmp_path):
    import artifact as m

    a = m.build_sealed_artifact()
    path = tmp_path / "seal.json"
    a.save(path)
    loaded = m.SealedArtifact.load(path)
    assert loaded.semantic_hash() == a.semantic_hash()


def test_sources_section_records_all_six_pinned_authority_hashes():
    import artifact as m

    a = m.build_sealed_artifact()
    s = a.sources
    assert s.preregistration_doc_sha256 == (
        "67b5d3c2255dd7c8b7dbc8aa8cbb44e467dc1e104d852e28edb36b818a84d349"
    )
    assert s.params_seal_draft_doc_sha256 == (
        "dc9232ef73dfca733a77bc89ec7cbb825f0a692e29707915372ae39b6b0fb140"
    )
    assert s.universe_map_sha256 == (
        "512285ebf67bb49dc1844d7c76dda4ea09dc19cbfb5968d32caee4a688cae8b2"
    )
    assert s.spread_census_sha256 == (
        "10d5a1c52c77d6c2a1ce81adb4776fec69aefdcc2dbc7e87f08672b185113609"
    )
    assert s.basis_analysis_full_sha256 == (
        "835e2abea219d3e78eec21f7ef64d939d7945ca764e3684136f41287e9b0378c"
    )
    assert s.fee_probe_sha256 == (
        "b94532dcd3c2cc8aa04a137c6471ff3ffa6d2ba4dffca3af3c287ca7b1532a5d"
    )


def test_dataset_manifest_hash_is_an_honest_none_not_a_fabricated_value():
    """H1's actual Binance archive corpus (a real one-time, operator-approved
    network collection, H1 AC25) has not been produced in this environment —
    inventing a plausible-looking hash here would be worse than leaving it
    unset. H3/H4 populate this once a real corpus manifest exists."""
    import artifact as m

    a = m.build_sealed_artifact()
    assert a.sources.dataset_manifest_hash is None


def test_semantic_hash_changes_if_a_config_threshold_is_tampered():
    import dataclasses as dc

    import artifact as m
    import configs as c

    a = m.build_sealed_artifact()
    tampered_configs = list(a.configs)
    bumped = dc.replace(
        tampered_configs[0],
        params={**tampered_configs[0].params, "threshold": 0.006},
    )
    bumped = dc.replace(
        bumped,
        canonical_hash=c.canonical_config_hash(
            bumped.config_id, bumped.family, bumped.params
        ),
    )
    tampered_configs[0] = bumped
    tampered = dc.replace(a, configs=tuple(tampered_configs))
    assert tampered.semantic_hash() != a.semantic_hash()


def test_semantic_hash_matches_the_pinned_h2_lock_digest():
    """ROB-1060 H2-lock item 1 (root cause fix): every sealed VALUE was
    independently re-derived and found correct at seal time, but the
    semantic hash summarizing all of them was pinned NOWHERE -- so a fully
    green 69-test suite let 17 of 37 mutations silently move it. THIS test is
    the actual lock. `SEALED_ARTIFACT_SEMANTIC_HASH` is a module constant;
    changing it is a deliberate re-seal, never a routine edit to make a
    failing test pass.

    2026-07-26 adversarial-verification Finding 2: the digest was re-sealed
    to COVER the 11 ROB-846 identity components (previously outside it) --
    see `test_pre_coverage_extension_semantic_hash_still_matches_the_
    original_h2_lock_digest` for proof that no sealed VALUE changed, only
    digest coverage."""
    import artifact as m

    a = m.build_sealed_artifact()
    assert a.semantic_hash() == m.SEALED_ARTIFACT_SEMANTIC_HASH
    # Also compared against a bare literal (not merely the constant under
    # test) so the pin itself cannot silently drift with the module.
    assert (
        m.SEALED_ARTIFACT_SEMANTIC_HASH
        == "6ed1656501766f9e026048d0a725a669b21d8ae16225c475c5bb321a2265e8e8"
    )


def test_pre_coverage_extension_semantic_hash_still_matches_the_original_h2_lock_digest():
    """Proof for the Finding-2 re-seal: recomputing the digest with ONLY the
    ORIGINAL coverage (`{configs, params, sources}`, no identity components)
    against the CURRENT (never-changed) sealed configs/params/sources still
    equals the original 2026-07-25 pin, byte-for-byte -- the coverage
    extension changed WHAT the digest covers, never any sealed VALUE inside
    it."""
    import artifact as m

    a = m.build_sealed_artifact()
    assert (
        a.semantic_hash_pre_coverage_extension()
        == m.PRE_COVERAGE_EXTENSION_SEMANTIC_HASH
        == "b0456239ba5893208c30f93c3a58a7f2ecb2a28800cfbdefc150124e771508e0"
    )
    # And the pre-/post-coverage-extension digests are (necessarily)
    # different values -- the extension is a real, non-vacuous change to
    # what the digest covers, not a no-op.
    assert a.semantic_hash_pre_coverage_extension() != a.semantic_hash()


def test_identity_components_section_covers_every_config_and_is_a_pure_function_of_configs_and_params():
    """The digest coverage extension itself: `to_dict()["identity_components"]`
    must key every one of the 16 configs and carry each config's full
    11-component ROB-846 identity -- and be reproducible byte-for-byte
    (proving it introduces no hidden state)."""
    import artifact as m

    a = m.build_sealed_artifact()
    d = a.to_dict()
    assert set(d["identity_components"]) == {c.config_id for c in a.configs}
    from research_contracts.canonical_hash import IDENTITY_COMPONENTS

    for config_id, components in d["identity_components"].items():
        assert set(components) == set(IDENTITY_COMPONENTS), config_id
    assert a.to_dict() == m.build_sealed_artifact().to_dict()


def test_constructing_artifact_with_tampered_sealed_effective_n_is_rejected():
    """``SealedArtifact.__post_init__`` re-validates the universe invariant on
    every construction — a tampered ``sealed_effective_n`` is rejected at
    construction time (fail-closed), stronger than merely producing a
    different hash."""
    import dataclasses as dc

    import artifact as m
    import params as prm

    a = m.build_sealed_artifact()
    tampered_universe = dc.replace(a.params.universe, sealed_effective_n=21)
    tampered_params = dc.replace(a.params, universe=tampered_universe)
    with pytest.raises(prm.UniverseSealError, match="20"):
        dc.replace(a, params=tampered_params)


# --------------------------------------------------------------------------- #
# ROB-1060 H2-lock item 8: `load()` must be tamper-evident -- a saved         #
# artifact JSON edited to relax a sealed value must fail to load, not         #
# silently succeed.                                                           #
# --------------------------------------------------------------------------- #


def test_load_rejects_a_config_whose_params_were_tampered_leaving_a_stale_canonical_hash(
    tmp_path,
):
    import json as _json

    import artifact as m

    a = m.build_sealed_artifact()
    path = tmp_path / "seal.json"
    a.save(path)
    d = _json.loads(path.read_text())
    # Tamper AP-A1-00's threshold but leave the recorded canonical_hash
    # untouched (stale) -- exactly the "config case is worst" scenario the
    # H2-lock verification flagged.
    d["configs"]["AP-A1-00"]["params"]["threshold"] = 0.010
    path.write_text(_json.dumps(d))
    with pytest.raises(m.ArtifactIntegrityError, match="AP-A1-00"):
        m.SealedArtifact.load(path)


def test_load_rejects_a_semantic_hash_mismatch_against_an_expected_digest(tmp_path):
    import json as _json

    import artifact as m

    a = m.build_sealed_artifact()
    original_hash = a.semantic_hash()
    path = tmp_path / "seal.json"
    a.save(path)
    d = _json.loads(path.read_text())
    # Relax AP-A2's symbol_concentration_pct 35 -> 40 -- every per-config
    # canonical_hash stays internally consistent with its own (untouched)
    # params, so only the semantic-digest check (against an expected value)
    # can catch this.
    for cond in d["params"]["gate_thresholds"]["ap_a2"]:
        if cond["metric"] == "symbol_concentration_pct":
            cond["value"] = 40
    path.write_text(_json.dumps(d))
    with pytest.raises(m.ArtifactIntegrityError, match="semantic hash mismatch"):
        m.SealedArtifact.load(path, expected_semantic_hash=original_hash)


def test_load_with_expected_hash_succeeds_when_untampered(tmp_path):
    import artifact as m

    a = m.build_sealed_artifact()
    path = tmp_path / "seal.json"
    a.save(path)
    loaded = m.SealedArtifact.load(path, expected_semantic_hash=a.semantic_hash())
    assert loaded.semantic_hash() == a.semantic_hash()

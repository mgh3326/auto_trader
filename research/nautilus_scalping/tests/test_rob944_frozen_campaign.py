"""ROB-944 (H4, ROB-940) — acyclic full-campaign identity envelope tests.

Q4 (orch-fable-answer-rob944-20260717.md, final): acyclic envelope -- freeze
components/hashes first, build the 24 H6 specs from them, build ONE top-level
envelope, hash it ONCE; the final hash is never fed back into its own inputs.
Covers RED/regression matrix item 10: pure --plan determinism + every frozen
subtree changing the hash when mutated, plus captain-audit item 3 (deep-copy
sealing against post-build mutation of shared/injected dicts).
"""

from __future__ import annotations

import pytest
import rob941_frozen_scope as frozen
import rob944_folds as foldmod
import rob946_campaign_identity as identity
from rob944_frozen_campaign import (
    CANONICAL_ROW_ORDER,
    H1_MANIFEST_EXPECTED_CONTENT_HASH,
    H3_MANIFEST_EXPECTED_HASH,
    PRODUCTION_S1_STRATEGY_KEY,
    PRODUCTION_S1_STRATEGY_VERSION,
    PRODUCTION_S2_STRATEGY_KEY,
    PRODUCTION_S2_STRATEGY_VERSION,
    SCENARIO_EXECUTION_SEMANTICS,
    FrozenCampaignEnvelope,
    FrozenCampaignError,
    RowContentTamperError,
    RowOrderError,
    _assert_row_matches_frozen_h3_manifest,
    build_frozen_campaign_envelope,
    build_production_campaign_config_rows,
    build_production_frozen_campaign_envelope,
    build_production_strategy_sources,
    load_production_dataset_manifest,
)

_FAKE_HASH = "ab" * 32


def _fake_source(key, version, text):
    return identity.StrategySourceProvenance(
        strategy_key=key, strategy_version=version, source_text=text
    )


def _fake_rows():
    """Captain boundary correction (2026-07-17): H4 owns exactly ONE frozen
    production envelope, not a generic fake-row generator (that already
    exists as H6's ``rob946_campaign_identity``) -- ``build_frozen_campaign_envelope``
    now verifies EVERY row's params/hypothesis against the frozen H3 manifest
    before building specs/IDs, so these "fake" fixtures use the REAL frozen
    24 rows. Only genuinely swappable, non-row components (sources, dataset
    manifest, execution-code files) remain injectable fakes below."""
    return build_production_campaign_config_rows()


def _synthetic_rows_bypassing_content_check():
    """The OLD synthetic-param row generator -- kept only for the row-ORDER/
    membership tests (missing/duplicate/13th/tampered-id), which must fail
    on config_id shape/order alone, before content verification even runs."""
    rows = []
    for i in range(12):
        rows.append(
            identity.CampaignConfigRow(
                config_id=f"S1-{i:02d}", params={"L": 16 + i}, hypothesis=f"s1-{i}"
            )
        )
    for i in range(12):
        rows.append(
            identity.CampaignConfigRow(
                config_id=f"S2-{i:02d}", params={"z_min": 3.0 + i}, hypothesis=f"s2-{i}"
            )
        )
    return rows


def _fake_dataset_manifest():
    return {"symbols": ["BTCUSDT"], "rows": 100}


def _fake_sources():
    return {
        "S1": _fake_source("FAKE-S1", "v1", "def s1(): ..."),
        "S2": _fake_source("FAKE-S2", "v2", "def s2(): ..."),
    }


def _fake_fold_schedule():
    return foldmod.generate_fold_schedule(
        0,
        foldmod.TRAIN_MS + foldmod.EMBARGO_MS + foldmod.OOS_MS + 5 * foldmod.ROLL_MS,
    )


def _build_fake_envelope(dataset_manifest=None, signal_hash=_FAKE_HASH):
    dataset_manifest = dataset_manifest or _fake_dataset_manifest()
    dm_hash = identity.canonical_hash.canonical_sha256(dataset_manifest)
    return build_frozen_campaign_envelope(
        config_rows=_fake_rows(),
        sources=_fake_sources(),
        dataset_manifest=dataset_manifest,
        dataset_manifest_expected_hash=dm_hash,
        fold_schedule=_fake_fold_schedule(),
        signal_manifest_hash_value=signal_hash,
        expected_signal_manifest_hash=signal_hash,
    )


# ---------------------------------------------------------------------------
# builder mechanics
# ---------------------------------------------------------------------------


def test_build_frozen_campaign_envelope_produces_stable_deterministic_hash():
    env1 = _build_fake_envelope()
    env2 = _build_fake_envelope()
    assert env1.full_campaign_hash() == env2.full_campaign_hash()
    assert len(env1.full_campaign_hash()) == 64


def test_builder_rejects_stale_signal_manifest_hash_pin():
    with pytest.raises(FrozenCampaignError):
        build_frozen_campaign_envelope(
            config_rows=_fake_rows(),
            sources=_fake_sources(),
            dataset_manifest=_fake_dataset_manifest(),
            dataset_manifest_expected_hash=identity.canonical_hash.canonical_sha256(
                _fake_dataset_manifest()
            ),
            fold_schedule=_fake_fold_schedule(),
            signal_manifest_hash_value="stale-actual-value",
            expected_signal_manifest_hash="pinned-expected-value",
        )


def test_builder_rejects_stale_dataset_manifest_hash_pin():
    with pytest.raises(identity.DatasetManifestHashMismatchError):
        build_frozen_campaign_envelope(
            config_rows=_fake_rows(),
            sources=_fake_sources(),
            dataset_manifest=_fake_dataset_manifest(),
            dataset_manifest_expected_hash="0" * 64,  # wrong on purpose
            fold_schedule=_fake_fold_schedule(),
            signal_manifest_hash_value=_FAKE_HASH,
            expected_signal_manifest_hash=_FAKE_HASH,
        )


def test_builder_seals_dataset_manifest_against_post_build_mutation():
    """Captain audit item 3: mutating the CALLER's own dataset_manifest dict
    after the envelope was built must NOT change subsequent hash calls --
    the envelope must have deep-copied at construction, not aliased."""
    dataset_manifest = _fake_dataset_manifest()
    env = _build_fake_envelope(dataset_manifest=dataset_manifest)
    hash_before = env.full_campaign_hash()

    dataset_manifest["rows"] = 999999  # mutate the ORIGINAL caller-owned dict
    dataset_manifest["symbols"].append("MUTATED")

    hash_after = env.full_campaign_hash()
    assert hash_before == hash_after  # the envelope's own copy is unaffected

    # A genuinely NEW envelope built from the mutated dict DOES differ --
    # proving the sealing test above isn't accidentally insensitive.
    new_dm_hash = identity.canonical_hash.canonical_sha256(dataset_manifest)
    new_env = build_frozen_campaign_envelope(
        config_rows=_fake_rows(),
        sources=_fake_sources(),
        dataset_manifest=dataset_manifest,
        dataset_manifest_expected_hash=new_dm_hash,
        fold_schedule=_fake_fold_schedule(),
        signal_manifest_hash_value=_FAKE_HASH,
        expected_signal_manifest_hash=_FAKE_HASH,
    )
    assert new_env.full_campaign_hash() != hash_before


# ---------------------------------------------------------------------------
# Q4 addendum (captain, 2026-07-17): the acyclic envelope must contain the
# exact ordered 24 experiment IDs as an explicit, hashed component --
# component hashes -> IDs -> top payload/hash, never a cyclic dependency.
# ---------------------------------------------------------------------------


def test_envelope_carries_exactly_24_unique_experiment_ids_in_row_order():
    env = _build_fake_envelope()
    plain = env.to_dict()
    ids = plain["experiment_ids"]
    assert len(ids) == 24
    assert len(set(ids)) == 24
    # Order/membership contract: index-for-index correspondence with rows,
    # independently re-derived here via the SAME research_contracts authority
    # (not merely trusting the envelope's own internal computation).
    for row, exp_id in zip(plain["rows"], ids, strict=True):
        hashes = identity.canonical_hash.compute_identity_hashes(row["components"])
        expected = identity.canonical_hash.derive_experiment_id(
            row["strategy_key"], row["strategy_version"], hashes
        )
        assert exp_id == expected


def test_experiment_ids_are_derived_before_the_top_level_hash_not_fed_back_into_it():
    """The IDs are a pure function of already-frozen components (never of
    full_campaign_hash itself) -- two envelopes built from identical inputs
    must derive byte-identical experiment_ids AND byte-identical
    full_campaign_hash, with no circularity between the two."""
    env_a = _build_fake_envelope()
    env_b = _build_fake_envelope()
    assert env_a.to_dict()["experiment_ids"] == env_b.to_dict()["experiment_ids"]
    assert env_a.full_campaign_hash() == env_b.full_campaign_hash()


def _envelope_kwargs_for(rows):
    dataset_manifest = _fake_dataset_manifest()
    dm_hash = identity.canonical_hash.canonical_sha256(dataset_manifest)
    return {
        "config_rows": rows,
        "sources": _fake_sources(),
        "dataset_manifest": dataset_manifest,
        "dataset_manifest_expected_hash": dm_hash,
        "fold_schedule": _fake_fold_schedule(),
        "signal_manifest_hash_value": _FAKE_HASH,
        "expected_signal_manifest_hash": _FAKE_HASH,
    }


def test_canonical_row_order_constant_is_s1_00_to_11_then_s2_00_to_11():
    assert CANONICAL_ROW_ORDER == (
        *(f"S1-{i:02d}" for i in range(12)),
        *(f"S2-{i:02d}" for i in range(12)),
    )


def test_reordered_rows_fail_closed_instead_of_silently_reordering_ids():
    """Prompt RED #4: exact ordered H3 membership -- reorder (a swap here)
    must be REJECTED, not silently accepted with a merely-different
    experiment_ids order/top hash."""
    rows = _synthetic_rows_bypassing_content_check()
    reordered = [rows[1], rows[0], *rows[2:]]
    with pytest.raises(RowOrderError):
        build_frozen_campaign_envelope(**_envelope_kwargs_for(reordered))


def test_missing_row_fails_closed():
    rows = _synthetic_rows_bypassing_content_check()[
        :-1
    ]  # 23 rows -- last S2 config missing
    with pytest.raises((RowOrderError, identity.CampaignIdentityError)):
        build_frozen_campaign_envelope(**_envelope_kwargs_for(rows))


def test_duplicate_row_fails_closed():
    rows = _synthetic_rows_bypassing_content_check()[:-1]
    rows.append(rows[0])  # duplicate S1-00 instead of the missing S2-11
    with pytest.raises((RowOrderError, identity.CampaignIdentityError)):
        build_frozen_campaign_envelope(**_envelope_kwargs_for(rows))


def test_a_13th_extra_row_fails_closed():
    rows = _synthetic_rows_bypassing_content_check()
    rows.append(
        identity.CampaignConfigRow(
            config_id="S1-12", params={"L": 99}, hypothesis="extra"
        )
    )
    with pytest.raises((RowOrderError, identity.CampaignIdentityError)):
        build_frozen_campaign_envelope(**_envelope_kwargs_for(rows))


def test_tampered_config_id_fails_closed():
    rows = _synthetic_rows_bypassing_content_check()
    rows[0] = identity.CampaignConfigRow(
        config_id="S1-TAMPERED", params=rows[0].params, hypothesis=rows[0].hypothesis
    )
    with pytest.raises((RowOrderError, identity.CampaignIdentityError)):
        build_frozen_campaign_envelope(**_envelope_kwargs_for(rows))


# ---------------------------------------------------------------------------
# captain exact-membership addendum (2026-07-17): row ORDER alone is not
# enough -- a same-domain param swap or altered hypothesis under an
# otherwise-correct config_id must fail closed too. These exercise the
# production-scoped content-verification helper directly (never the generic
# build_frozen_campaign_envelope, which legitimately accepts fake test rows).
# ---------------------------------------------------------------------------


def test_production_rows_pass_their_own_content_self_check():
    for row in build_production_campaign_config_rows():
        _assert_row_matches_frozen_h3_manifest(row)  # must not raise


def test_s1_param_swap_with_correct_config_id_fails_closed():
    rows = build_production_campaign_config_rows()
    real = rows[0]
    assert real.config_id == "S1-00"
    tampered = identity.CampaignConfigRow(
        config_id=real.config_id,
        params={**real.params, "k_SL": real.params["k_SL"] + 0.5},  # in-domain swap
        hypothesis=real.hypothesis,
    )
    with pytest.raises(RowContentTamperError):
        _assert_row_matches_frozen_h3_manifest(tampered)


def test_s1_hypothesis_tamper_with_correct_config_id_and_params_fails_closed():
    rows = build_production_campaign_config_rows()
    real = rows[0]
    tampered = identity.CampaignConfigRow(
        config_id=real.config_id,
        params=real.params,
        hypothesis="a tampered hypothesis",
    )
    with pytest.raises(RowContentTamperError):
        _assert_row_matches_frozen_h3_manifest(tampered)


def test_s2_param_swap_with_correct_config_id_fails_closed():
    rows = build_production_campaign_config_rows()
    real = next(r for r in rows if r.config_id == "S2-00")
    tampered = identity.CampaignConfigRow(
        config_id=real.config_id,
        params={**real.params, "z_min": real.params["z_min"] + 1.0},
        hypothesis=real.hypothesis,
    )
    with pytest.raises(RowContentTamperError):
        _assert_row_matches_frozen_h3_manifest(tampered)


def test_s2_hypothesis_tamper_with_correct_config_id_and_params_fails_closed():
    rows = build_production_campaign_config_rows()
    real = next(r for r in rows if r.config_id == "S2-00")
    tampered = identity.CampaignConfigRow(
        config_id=real.config_id,
        params=real.params,
        hypothesis="a tampered hypothesis",
    )
    with pytest.raises(RowContentTamperError):
        _assert_row_matches_frozen_h3_manifest(tampered)


def test_build_frozen_campaign_envelope_rejects_param_tamper_at_the_boundary():
    """Captain BLOCKER (2026-07-17): directly unit-testing the content-check
    helper is insufficient/vacuous on its own -- build_frozen_campaign_envelope
    ITSELF must call it, over every config_rows entry, before any
    spec/experiment_id is built. This exercises the real boundary: a
    same-domain param swap riding under a correct, correctly-ordered S1-00
    must be rejected by the builder, not merely by a helper nobody calls."""
    rows = list(build_production_campaign_config_rows())
    real = rows[0]
    assert real.config_id == "S1-00"
    rows[0] = identity.CampaignConfigRow(
        config_id=real.config_id,
        params={**real.params, "k_SL": real.params["k_SL"] + 0.5},
        hypothesis=real.hypothesis,
    )
    with pytest.raises(RowContentTamperError):
        build_frozen_campaign_envelope(**_envelope_kwargs_for(rows))


def test_build_frozen_campaign_envelope_rejects_hypothesis_tamper_at_the_boundary():
    rows = list(build_production_campaign_config_rows())
    real = rows[0]
    rows[0] = identity.CampaignConfigRow(
        config_id=real.config_id,
        params=real.params,
        hypothesis="a tampered hypothesis",
    )
    with pytest.raises(RowContentTamperError):
        build_frozen_campaign_envelope(**_envelope_kwargs_for(rows))


def test_build_frozen_campaign_envelope_accepts_the_real_untampered_24_rows():
    """The positive control for the two tests above -- the boundary check
    must not be a false-positive trap that rejects legitimate production
    rows too."""
    env = build_frozen_campaign_envelope(
        **_envelope_kwargs_for(build_production_campaign_config_rows())
    )
    assert len(env.to_dict()["rows"]) == 24


def test_production_config_rows_are_in_the_exact_canonical_order():
    """Production plan assertion: the real frozen 24 rows must already
    satisfy the canonical order contract -- build_production_frozen_campaign_envelope
    must never itself trip the RowOrderError guard."""
    rows = build_production_campaign_config_rows()
    assert tuple(r.config_id for r in rows) == CANONICAL_ROW_ORDER
    env = build_production_frozen_campaign_envelope()
    assert tuple(r["strategy_key"] for r in env.to_dict()["rows"][:12]) == (
        (PRODUCTION_S1_STRATEGY_KEY,) * 12
    )
    assert tuple(r["strategy_key"] for r in env.to_dict()["rows"][12:]) == (
        (PRODUCTION_S2_STRATEGY_KEY,) * 12
    )


def test_builder_seals_mutable_row_components_against_post_build_mutation():
    rows = _fake_rows()
    env = _build_fake_envelope()
    hash_before = env.full_campaign_hash()
    rows[0].params["L"] = -99999  # mutate a CampaignConfigRow's params dict
    assert env.full_campaign_hash() == hash_before  # envelope already deep-copied


def test_to_dict_returned_structure_mutation_does_not_leak_back():
    env = _build_fake_envelope()
    hash_before = env.full_campaign_hash()
    payload = env.to_dict()
    payload["rows"][0]["components"]["params"]["L"] = -1
    payload["universe"].append("MUTATED")
    assert env.full_campaign_hash() == hash_before


# ---------------------------------------------------------------------------
# frozen-lineage correction: @dataclass(frozen=True) only blocks attribute
# REBINDING -- it does nothing to mutable dicts/lists living INSIDE those
# attributes. Direct nested-field mutation on the envelope itself must
# RAISE (never silently succeed and change a later full_campaign_hash()).
# ---------------------------------------------------------------------------


def test_direct_nested_dict_mutation_on_funding_pit_policy_raises():
    env = _build_fake_envelope()
    with pytest.raises(TypeError):
        env.funding_pit_policy["entry_gate"] = {}
    with pytest.raises(TypeError):
        env.funding_pit_policy["max_expected_cost_bps"] = 999


def test_direct_nested_dict_mutation_on_rows_components_raises():
    env = _build_fake_envelope()
    with pytest.raises(TypeError):
        env.rows[0]["components"]["params"]["L"] = -1
    with pytest.raises(TypeError):
        env.rows[0]["components"]["params"] = {}


def test_direct_nested_dict_mutation_on_h4_reason_contract_raises():
    env = _build_fake_envelope()
    with pytest.raises(TypeError):
        env.h4_reason_contract["selection"]["min_symbol_train_trades"] = 999


def test_direct_nested_mutation_attempts_never_change_the_hash():
    """Even setting aside whether mutation raises, the hash itself must be
    provably unaffected -- attempt every mutation inside a try/except and
    confirm the hash is identical before and after each attempt."""
    env = _build_fake_envelope()
    hash_before = env.full_campaign_hash()

    def _try(fn):
        try:
            fn()
        except (TypeError, AttributeError):
            pass

    def _set(mapping, key, value):
        mapping[key] = value

    _try(lambda: _set(env.funding_pit_policy, "max_expected_cost_bps", 999))
    _try(lambda: _set(env.rows[0]["components"]["params"], "L", -1))
    _try(lambda: _set(env.h3_fixed_constants, "ATR_PERIOD", -1))
    _try(lambda: _set(env.posture, "historical_screen_only", False))
    _try(lambda: _set(env.data_gap_policy, "reject_reason", "tampered"))
    _try(lambda: _set(env.execution_code_provenance, "x", "y"))
    _try(lambda: _set(env.fold_schedule[0], "fold_id", "tampered"))

    assert env.full_campaign_hash() == hash_before


def test_direct_construction_with_a_mutable_universe_list_is_sealed_too():
    """Captain freeze-audit addendum (2026-07-17, item B): __post_init__
    previously omitted ``universe`` from its freeze loop, so a caller passing
    ``universe=[...]`` (a plain, mutable list) directly could ``.append()``
    a fifth symbol post-construction and silently change a later
    full_campaign_hash() call on the SAME object. universe must now be
    normalized/sealed into an immutable tuple exactly like every other
    nested field, regardless of whether the caller passed a list or tuple."""
    kwargs = _base_kwargs()
    kwargs["universe"] = list(frozen.UNIVERSE)  # deliberately a mutable list
    env = FrozenCampaignEnvelope(**kwargs)
    hash_before = env.full_campaign_hash()

    with pytest.raises((TypeError, AttributeError)):
        env.universe.append("MUTATED")
    assert env.full_campaign_hash() == hash_before
    assert env.universe == tuple(frozen.UNIVERSE)


def test_frozen_envelope_constructed_directly_is_also_sealed():
    """The seal is enforced by __post_init__, universally -- regardless of
    whether the envelope was built via build_frozen_campaign_envelope or
    constructed directly with plain (caller-owned, mutable) dicts."""
    kwargs = _base_kwargs()
    original_policy = kwargs["funding_pit_policy"]
    env = FrozenCampaignEnvelope(**kwargs)
    hash_before = env.full_campaign_hash()

    # The CALLER's original dict is untouched by freezing (freeze copies).
    original_policy["max_expected_cost_bps"] = 999
    assert env.full_campaign_hash() == hash_before

    # And the envelope's OWN field is immutable too.
    with pytest.raises(TypeError):
        env.funding_pit_policy["max_expected_cost_bps"] = 999


# ---------------------------------------------------------------------------
# every frozen subtree changes the hash (constructed directly, not via
# the builder, to isolate the hash function itself)
# ---------------------------------------------------------------------------


def _base_kwargs():
    return {
        "schema_version": "v1",
        "window_start_iso": frozen.WINDOW_START_ISO,
        "window_end_iso": frozen.WINDOW_END_ISO,
        "universe": frozen.UNIVERSE,
        "dataset_manifest_hash": "a" * 64,
        "signal_manifest_hash": "b" * 64,
        "rows": ({"config_id": "S1-00"},),
        "experiment_ids": (
            "exp-0000000000000000000000000000000000000000000000000000000000000000",
        ),
        "fold_schedule": ({"fold_id": "fold-00"},),
        "scenario_execution": "independent_run_with_fresh_state",
        "funding_pit_policy": {"max_expected_cost_bps": 3.0},
        "data_gap_policy": {"reject_reason": "rejected:data_gap_in_position"},
        "posture": {"historical_screen_only": True},
        "execution_code_provenance": {"rob940_engine.py": "e" * 64},
        "h3_fixed_constants": {"ATR_PERIOD": 20},
        "h4_reason_contract": {"min_symbol_train_trades": 5},
    }


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("schema_version", "v2"),
        ("window_start_iso", "2020-01-01T00:00:00Z"),
        ("window_end_iso", "2020-02-01T00:00:00Z"),
        ("universe", ("BTCUSDT",)),
        ("dataset_manifest_hash", "c" * 64),
        ("signal_manifest_hash", "d" * 64),
        ("rows", ({"config_id": "S1-99"},)),
        (
            "experiment_ids",
            ("exp-9999999999999999999999999999999999999999999999999999999999999999",),
        ),
        ("fold_schedule", ({"fold_id": "fold-99"},)),
        ("scenario_execution", "shared_path_net_only_revaluation"),
        ("funding_pit_policy", {"max_expected_cost_bps": 999.0}),
        ("data_gap_policy", {"reject_reason": "something_else"}),
        ("posture", {"historical_screen_only": False}),
        ("execution_code_provenance", {"rob940_engine.py": "f" * 64}),
        ("h3_fixed_constants", {"ATR_PERIOD": 21}),
        ("h4_reason_contract", {"min_symbol_train_trades": 6}),
    ],
)
def test_mutating_every_subtree_changes_the_full_campaign_hash(field, new_value):
    base = FrozenCampaignEnvelope(**_base_kwargs())
    mutated_kwargs = _base_kwargs()
    mutated_kwargs[field] = new_value
    mutated = FrozenCampaignEnvelope(**mutated_kwargs)
    assert base.full_campaign_hash() != mutated.full_campaign_hash()


def test_identical_envelopes_hash_identically():
    a = FrozenCampaignEnvelope(**_base_kwargs())
    b = FrozenCampaignEnvelope(**_base_kwargs())
    assert a.full_campaign_hash() == b.full_campaign_hash()


# ---------------------------------------------------------------------------
# production wiring (real S1/S2 source, real committed H1 manifest fixture,
# real frozen H3 24-row manifest, real frozen fold schedule)
# ---------------------------------------------------------------------------


def test_production_strategy_identifiers_are_frozen_fable_promoted_values():
    assert PRODUCTION_S1_STRATEGY_KEY == "ROB940-S1-DONCHIAN-15M"
    assert PRODUCTION_S1_STRATEGY_VERSION == "s1-v1"
    assert PRODUCTION_S2_STRATEGY_KEY == "ROB940-S2-SHOCK-REVERSAL-5M"
    assert PRODUCTION_S2_STRATEGY_VERSION == "s2-v1"


def test_production_config_rows_are_exactly_the_frozen_24():
    rows = build_production_campaign_config_rows()
    ids = [r.config_id for r in rows]
    assert len(ids) == 24
    assert len(set(ids)) == 24


def test_load_production_dataset_manifest_matches_pinned_h1_hash():
    manifest = load_production_dataset_manifest()
    actual = identity.canonical_hash.canonical_sha256(manifest)
    assert actual == H1_MANIFEST_EXPECTED_CONTENT_HASH


def test_production_strategy_sources_hash_from_actual_files_and_reject_stale_pin():
    sources = build_production_strategy_sources()
    s1_actual = sources["S1"].verified_source_sha256()
    s2_actual = sources["S2"].verified_source_sha256()
    assert s1_actual != s2_actual  # distinct sources (H6 §1 contract)
    assert len(s1_actual) == 64

    with pytest.raises(identity.StrategySourceMismatchError):
        build_production_strategy_sources(expected_s1_source_sha256="0" * 64)[
            "S1"
        ].verified_source_sha256()


def test_build_production_frozen_campaign_envelope_is_deterministic_and_complete():
    env1 = build_production_frozen_campaign_envelope()
    env2 = build_production_frozen_campaign_envelope()
    assert env1.full_campaign_hash() == env2.full_campaign_hash()
    assert len(env1.rows) == 24
    assert env1.signal_manifest_hash == H3_MANIFEST_EXPECTED_HASH
    assert env1.dataset_manifest_hash == H1_MANIFEST_EXPECTED_CONTENT_HASH
    assert env1.scenario_execution == SCENARIO_EXECUTION_SEMANTICS
    assert len(env1.fold_schedule) == 8  # the pinned frozen-window fold count


def test_build_production_frozen_campaign_envelope_rejects_tampered_signal_hash_pin():
    with pytest.raises(FrozenCampaignError):
        build_production_frozen_campaign_envelope(
            expected_signal_manifest_hash="tampered" + H3_MANIFEST_EXPECTED_HASH[8:]
        )


# ---------------------------------------------------------------------------
# captain audit supplement (2026-07-17): execution code provenance, H3 fixed
# constants, H4 reason contract -- build_campaign_experiment_specs' per-row
# "code" component only binds S1/S2 STRATEGY source, not the shared H2
# execution engine or H4's own runner/controller/CLI, nor H3's fixed
# (non-tunable) constants/spec-deviation text, nor H4's own reason/threshold
# contract. These three components close that gap.
# ---------------------------------------------------------------------------


def test_execution_code_provenance_hashes_actual_current_file_bytes():
    import hashlib

    from rob944_frozen_campaign import (
        EXECUTION_CODE_LOGICAL_FILES,
        build_execution_code_provenance_component,
    )

    provenance = build_execution_code_provenance_component()
    assert set(provenance) == {name for name, _path in EXECUTION_CODE_LOGICAL_FILES}
    for name, path in EXECUTION_CODE_LOGICAL_FILES:
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        assert provenance[name] == expected, name


def test_execution_code_provenance_is_injectable_and_sensitive_to_byte_content(
    tmp_path,
):
    from rob944_frozen_campaign import build_execution_code_provenance_component

    file_a = tmp_path / "some_module.py"
    file_a.write_text("x = 1\n")
    files = (("some_module.py", file_a),)
    hash_a = build_execution_code_provenance_component(files=files)["some_module.py"]

    file_a.write_text("x = 2\n")  # different byte content
    hash_b = build_execution_code_provenance_component(files=files)["some_module.py"]
    assert hash_a != hash_b


def test_execution_code_provenance_uses_stable_logical_names_not_absolute_paths():
    """Logical names may include a relative subdirectory prefix (e.g.
    ``app/services/...``) to disambiguate across directories, but must never
    be an ABSOLUTE filesystem path -- the resulting hash must not depend on
    where the repo happens to be checked out."""
    from rob944_frozen_campaign import build_execution_code_provenance_component

    provenance = build_execution_code_provenance_component()
    for name in provenance:
        assert not name.startswith("/")
        assert ":" not in name  # no Windows drive letters either


def test_h3_fixed_constants_component_matches_actual_frozen_signal_constants():
    from rob940_signal_manifest import FrozenSignalConstants
    from rob940_signal_s2 import SPEC_DEVIATIONS
    from rob944_frozen_campaign import build_h3_fixed_constants_component

    component = build_h3_fixed_constants_component()
    assert component["frozen_signal_constants"] == dict(FrozenSignalConstants._asdict())
    assert tuple(component["s2_spec_deviations"]) == SPEC_DEVIATIONS
    assert (
        component["s2_target_direction_invalid_reason_code"]
        == "target_direction_invalid"
    )


def test_h4_reason_contract_component_matches_actual_constants():
    from rob944_frozen_campaign import build_h4_reason_contract_component
    from rob944_gap_funding import (
        FUNDING_ENTRY_GATE_MAX_EXPECTED_COST_BPS,
        REASON_DATA_GAP_IN_POSITION,
        REASON_EXPECTED_FUNDING_COST_ABOVE_3BPS,
        REASON_FUNDING_EVIDENCE_UNAVAILABLE,
    )
    from rob944_selection import (
        INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
        INSUFFICIENT_SYMBOL_EVIDENCE_REASON,
        MIN_ELIGIBLE_SYMBOLS,
        MIN_SYMBOL_TRAIN_TRADES,
    )

    component = build_h4_reason_contract_component()
    assert component["selection"]["min_symbol_train_trades"] == MIN_SYMBOL_TRAIN_TRADES
    assert component["selection"]["min_eligible_symbols"] == MIN_ELIGIBLE_SYMBOLS
    assert (
        component["selection"]["insufficient_symbol_evidence_reason"]
        == INSUFFICIENT_SYMBOL_EVIDENCE_REASON
    )
    assert (
        component["selection"]["insufficient_eligible_symbols_reason"]
        == INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON
    )
    assert (
        component["funding_gate"]["max_expected_cost_bps"]
        == FUNDING_ENTRY_GATE_MAX_EXPECTED_COST_BPS
    )
    assert (
        component["funding_gate"]["evidence_unavailable_reason"]
        == REASON_FUNDING_EVIDENCE_UNAVAILABLE
    )
    assert (
        component["funding_gate"]["expected_cost_above_max_reason"]
        == REASON_EXPECTED_FUNDING_COST_ABOVE_3BPS
    )
    assert component["data_gap_reason"] == REASON_DATA_GAP_IN_POSITION


def test_production_envelope_includes_all_three_new_components_populated():
    env = build_production_frozen_campaign_envelope()
    assert len(env.execution_code_provenance) > 0
    assert all(len(v) == 64 for v in env.execution_code_provenance.values())
    assert env.h3_fixed_constants["frozen_signal_constants"]["ATR_PERIOD"] == 20
    assert env.h4_reason_contract["selection"]["min_symbol_train_trades"] == 5


def test_production_envelope_hash_changes_if_execution_code_base_dir_differs(tmp_path):
    """Proves the envelope's hash is genuinely sensitive to the execution
    code's actual bytes -- swap in a base_dir with DIFFERENT file content
    for the same logical names and confirm the hash moves."""
    from rob944_frozen_campaign import EXECUTION_CODE_LOGICAL_FILES

    alt_dir = tmp_path
    for name, _real_path in EXECUTION_CODE_LOGICAL_FILES:
        target = alt_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# stand-in content for {name}\n")

    env_real = build_production_frozen_campaign_envelope()
    env_alt = build_production_frozen_campaign_envelope(execution_code_base_dir=alt_dir)
    assert env_real.full_campaign_hash() != env_alt.full_campaign_hash()


def test_build_production_strategy_sources_verified_sha_equals_raw_bytes_sha():
    """Captain config/plan audit (2026-07-18, item B): each H6-verified
    source SHA-256 must equal ``hashlib.sha256(path.read_bytes()).hexdigest()``
    exactly -- proving ``read_bytes().decode("utf-8")`` is a lossless
    round-trip for these committed source files (no universal-newline
    translation silently altering what actually gets hashed, unlike
    ``Path.read_text()``)."""
    import hashlib

    from rob944_frozen_campaign import (
        _S1_SOURCE_PATH,
        _S2_SOURCE_PATH,
        build_production_strategy_sources,
    )

    sources = build_production_strategy_sources()
    assert (
        sources["S1"].verified_source_sha256()
        == hashlib.sha256(_S1_SOURCE_PATH.read_bytes()).hexdigest()
    )
    assert (
        sources["S2"].verified_source_sha256()
        == hashlib.sha256(_S2_SOURCE_PATH.read_bytes()).hexdigest()
    )
    assert (
        sources["S1"].verified_source_sha256() != sources["S2"].verified_source_sha256()
    )

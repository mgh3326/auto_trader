"""ROB-1060 H2 — RED-first: exactly-16 canonical config domain.

AP-A1 grid f in {14,21} x s in {56,84} x m in {28,56} = 8 configs
(threshold +-0.005 FIXED, not a parameter). AP-A2 grid L in {14,28} x k in
{5,6} x b in {1,2} = 8 configs (positive filter "Score > 0" FIXED). Total
EXACTLY 16, named AP-A1-00..07 / AP-A2-00..07. No 17th slot, ever.
"""

from __future__ import annotations

import math

import pytest


def test_build_all_configs_returns_exactly_16():
    import configs as m

    all_configs = m.build_all_configs()
    assert len(all_configs) == 16


def test_config_ids_are_exactly_the_expected_16_names():
    import configs as m

    ids = {c.config_id for c in m.build_all_configs()}
    expected = {f"AP-A1-{i:02d}" for i in range(8)} | {
        f"AP-A2-{i:02d}" for i in range(8)
    }
    assert ids == expected


def test_ap_a1_grid_is_the_full_cartesian_product_f_s_m():
    import configs as m

    a1 = m.build_ap_a1_configs()
    assert len(a1) == 8
    grid = {(c.params["f"], c.params["s"], c.params["m"]) for c in a1}
    expected = {(f, s, mm) for f in (14, 21) for s in (56, 84) for mm in (28, 56)}
    assert grid == expected


def test_ap_a1_threshold_is_fixed_at_point_005_for_every_config():
    import configs as m

    for c in m.build_ap_a1_configs():
        assert c.params["threshold"] == pytest.approx(0.005)
        assert math.isclose(c.params["threshold"], 0.005, rel_tol=0, abs_tol=1e-18)


def test_ap_a2_grid_is_the_full_cartesian_product_l_k_b():
    import configs as m

    a2 = m.build_ap_a2_configs()
    assert len(a2) == 8
    grid = {(c.params["L"], c.params["k"], c.params["b"]) for c in a2}
    expected = {(ell, k, b) for ell in (14, 28) for k in (5, 6) for b in (1, 2)}
    assert grid == expected


def test_ap_a2_positive_filter_is_fixed_score_gt_0_for_every_config():
    import configs as m

    for c in m.build_ap_a2_configs():
        assert c.params["positive_filter"] == "Score > 0"


def test_every_config_has_a_64_hex_canonical_hash_and_all_are_unique():
    import configs as m

    all_configs = m.build_all_configs()
    hashes = [c.canonical_hash for c in all_configs]
    assert len(hashes) == len(set(hashes)) == 16
    for h in hashes:
        assert len(h) == 64
        int(h, 16)  # raises if not hex


def test_1_ulp_threshold_variation_changes_the_hash():
    import configs as m

    base = m.build_ap_a1_configs()[0]
    mutated_params = dict(base.params)
    mutated_params["threshold"] = math.nextafter(0.005, 1.0)
    mutated_hash = m.canonical_config_hash(base.config_id, base.family, mutated_params)
    assert mutated_hash != base.canonical_hash


def test_key_order_permutation_of_params_does_not_change_the_hash():
    import configs as m

    base = m.build_ap_a1_configs()[0]
    reordered = dict(reversed(list(base.params.items())))
    reordered_hash = m.canonical_config_hash(base.config_id, base.family, reordered)
    assert reordered_hash == base.canonical_hash


# --------------------------------------------------------------------------- #
# ROB-1060 H2-lock item 6: set-based grid assertions are invariant to         #
# enumeration order, so reordering `_AP_A1_F` (etc.) silently reassigns which #
# grid point each `AP-A1-NN` id names, while `test_ap_a1_grid_is_the_full_    #
# cartesian_product_f_s_m` still passes. Pin the ORDERED id -> params         #
# mapping so the index<->grid-point contract the module docstring promises   #
# is actually enforced.                                                      #
# --------------------------------------------------------------------------- #


def test_ap_a1_config_ids_map_to_the_pinned_ordered_grid_points():
    import configs as m

    expected = [
        ("AP-A1-00", {"f": 14, "s": 56, "m": 28, "threshold": 0.005}),
        ("AP-A1-01", {"f": 14, "s": 56, "m": 56, "threshold": 0.005}),
        ("AP-A1-02", {"f": 14, "s": 84, "m": 28, "threshold": 0.005}),
        ("AP-A1-03", {"f": 14, "s": 84, "m": 56, "threshold": 0.005}),
        ("AP-A1-04", {"f": 21, "s": 56, "m": 28, "threshold": 0.005}),
        ("AP-A1-05", {"f": 21, "s": 56, "m": 56, "threshold": 0.005}),
        ("AP-A1-06", {"f": 21, "s": 84, "m": 28, "threshold": 0.005}),
        ("AP-A1-07", {"f": 21, "s": 84, "m": 56, "threshold": 0.005}),
    ]
    actual = [(c.config_id, dict(c.params)) for c in m.build_ap_a1_configs()]
    assert actual == expected


def test_ap_a2_config_ids_map_to_the_pinned_ordered_grid_points():
    import configs as m

    expected = [
        ("AP-A2-00", {"L": 14, "k": 5, "b": 1, "positive_filter": "Score > 0"}),
        ("AP-A2-01", {"L": 14, "k": 5, "b": 2, "positive_filter": "Score > 0"}),
        ("AP-A2-02", {"L": 14, "k": 6, "b": 1, "positive_filter": "Score > 0"}),
        ("AP-A2-03", {"L": 14, "k": 6, "b": 2, "positive_filter": "Score > 0"}),
        ("AP-A2-04", {"L": 28, "k": 5, "b": 1, "positive_filter": "Score > 0"}),
        ("AP-A2-05", {"L": 28, "k": 5, "b": 2, "positive_filter": "Score > 0"}),
        ("AP-A2-06", {"L": 28, "k": 6, "b": 1, "positive_filter": "Score > 0"}),
        ("AP-A2-07", {"L": 28, "k": 6, "b": 2, "positive_filter": "Score > 0"}),
    ]
    actual = [(c.config_id, dict(c.params)) for c in m.build_ap_a2_configs()]
    assert actual == expected


# --------------------------------------------------------------------------- #
# ROB-1060 H2-lock item 7: `config.params` must be genuinely immutable        #
# through the object -- `frozen=True` blocks `config.params = ...` but not    #
# `config.params["threshold"] = ...`, which would rot `canonical_hash`        #
# without re-deriving it.                                                     #
# --------------------------------------------------------------------------- #


def test_config_params_dict_is_immutable_through_the_object():
    import configs as m

    config = m.build_ap_a1_configs()[0]
    with pytest.raises(TypeError, match="does not support item assignment"):
        config.params["threshold"] = 0.010


def test_build_all_configs_is_reproducible_byte_identical_across_calls():
    import configs as m

    first = [(c.config_id, c.canonical_hash) for c in m.build_all_configs()]
    second = [(c.config_id, c.canonical_hash) for c in m.build_all_configs()]
    assert first == second


# --------------------------------------------------------------------------- #
# Mutation-provable guards: 17th slot, duplicates, cross-family supersedes.    #
# --------------------------------------------------------------------------- #


def test_validate_config_domain_rejects_a_17th_slot():
    import configs as m

    configs_list = list(m.build_all_configs())
    seventeenth = m.ConfigSpec(
        config_id="AP-A1-08",
        family="AP-A1",
        params={"f": 14, "s": 56, "m": 28, "threshold": 0.005},
        canonical_hash=m.canonical_config_hash(
            "AP-A1-08", "AP-A1", {"f": 14, "s": 56, "m": 28, "threshold": 0.005}
        ),
    )
    configs_list.append(seventeenth)
    with pytest.raises(m.ConfigCountError, match="17"):
        m.validate_config_domain(tuple(configs_list))


def test_validate_config_domain_rejects_a_duplicate_config_id():
    import configs as m

    configs_list = list(m.build_all_configs())
    configs_list[1] = configs_list[0]  # exact duplicate row
    # Still 16 rows in length but only 15 distinct ids -- must still fail closed.
    corrupted = tuple(configs_list[:-1])  # drop one to keep count at 16... no:
    # Build a version that keeps count at 16 but duplicates one id.
    corrupted = (configs_list[0], *configs_list[1:])
    assert len(corrupted) == 16
    with pytest.raises(m.DuplicateConfigError, match="duplicate config_id"):
        m.validate_config_domain(corrupted)


def test_assert_no_seventeenth_slot_is_structural_not_just_a_count_check():
    """The builder itself takes no extension parameter -- there is no
    caller-reachable way to ask ``build_all_configs`` for a 17th config."""
    import inspect

    import configs as m

    sig = inspect.signature(m.build_all_configs)
    assert len(sig.parameters) == 0


def test_supersedes_lineage_check_rejects_cross_family_supersession():
    import configs as m

    a1 = m.build_ap_a1_configs()[0]
    a2 = m.build_ap_a2_configs()[0]
    with pytest.raises(m.CrossFamilySupersedesError, match="cannot supersede"):
        m.assert_valid_supersedes(child=a2, parent=a1)


def test_supersedes_lineage_check_allows_same_family_supersession():
    import configs as m

    a1_0 = m.build_ap_a1_configs()[0]
    a1_1 = m.build_ap_a1_configs()[1]
    m.assert_valid_supersedes(child=a1_1, parent=a1_0)  # must not raise

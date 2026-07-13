"""ROB-846 — canonical identity helpers (unit).

The experiment registry pins strategy/code/params/dataset/PIT/frozen-config/
policy/benchmark/cost/MDD to ONE closed, typed, collision-free canonical AST so
a strategy version can be reproduced exactly and two different identities can
never share an experiment_id. These tests lock the canonical form down.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.services.research_canonical_hash import (
    IDENTITY_COMPONENTS,
    canonical_json,
    canonical_sha256,
    compute_identity_hashes,
    compute_identity_hashes_from_ast,
    derive_experiment_id,
    encode_canonical,
    encode_manifest,
    hash_canonical_ast,
)

pytestmark = pytest.mark.unit

_HEX64 = 64


def _identity_components() -> dict[str, object]:
    return {
        "strategy": {"name": "NostalgiaForInfinity", "class": "NFIX"},
        "code": "def populate_entry_trend(): ...",
        "params": {"roi": {"0": 0.05}, "stoploss": -0.1},
        "dataset_manifest": {"pairs": ["BTC/USDT"], "candles": 200_000},
        "universe": ["BTC/USDT", "ETH/USDT"],
        "pit": {"information_cutoff": "2026-01-01T00:00:00Z"},
        "frozen_config": {"max_open_trades": 5, "timeframe": "5m"},
        "policy": {"gate": "honest_offline_v1"},
        "benchmark": {"symbol": "BTC/USDT", "kind": "buy_and_hold"},
        "cost": {"maker_bps": 2, "taker_bps": 4, "slippage_bps": 3},
        "mdd": {"definition": "peak_to_trough", "window": "full"},
    }


def test_canonical_sha256_is_deterministic_lowercase_hex() -> None:
    digest = canonical_sha256({"b": 2, "a": 1})
    assert digest == canonical_sha256({"b": 2, "a": 1})
    assert len(digest) == _HEX64
    assert digest == digest.lower()
    assert all(c in "0123456789abcdef" for c in digest)


def test_canonical_json_is_key_order_independent() -> None:
    assert canonical_json({"a": 1, "b": {"x": 1, "y": 2}}) == canonical_json(
        {"b": {"y": 2, "x": 1}, "a": 1}
    )
    assert canonical_sha256({"a": 1, "b": 2}) == canonical_sha256({"b": 2, "a": 1})


def test_encode_canonical_is_a_closed_typed_ast() -> None:
    # Every value becomes a tagged [tag, payload] node built of JSON-native types.
    assert encode_canonical("x") == ["str", "x"]
    assert encode_canonical(5) == ["int", 5]
    assert encode_canonical(True) == ["bool", True]
    assert encode_canonical(None) == ["null", None]
    assert encode_canonical(Decimal("1.0")) == ["decimal", "1.0"]
    assert encode_canonical(["a"]) == ["list", [["str", "a"]]]
    assert encode_canonical(("a",)) == ["tuple", [["str", "a"]]]
    assert encode_canonical({"a"}) == ["set", [["str", "a"]]]
    assert encode_canonical({"k": 1}) == ["dict", [["k", ["int", 1]]]]
    # The AST is JSON-serialisable (JSONB-safe) by construction.
    json.dumps(encode_canonical(_identity_components()))


def test_distinct_payloads_hash_differently() -> None:
    assert canonical_sha256({"roi": 0.05}) != canonical_sha256({"roi": 0.06})


def test_decimal_hashes_deterministically_as_typed_node() -> None:
    payload = {"pf": Decimal("1.30"), "at": datetime(2026, 1, 1, tzinfo=UTC)}
    assert canonical_sha256(payload) == canonical_sha256(payload)
    # Decimal is a typed "decimal" node carrying its exact text (1.30 != 1.3).
    assert '["decimal","1.30"]' in canonical_json(payload)


def test_compute_identity_hashes_covers_every_component() -> None:
    hashes = compute_identity_hashes(_identity_components())
    expected_keys = {f"{name}_hash" for name in IDENTITY_COMPONENTS}
    assert set(hashes) == expected_keys
    for value in hashes.values():
        assert len(value) == _HEX64


def test_identity_hashes_are_independent_per_component() -> None:
    base = _identity_components()
    base_hashes = compute_identity_hashes(base)

    mutated = _identity_components()
    mutated["params"] = {"roi": {"0": 0.99}, "stoploss": -0.1}
    mutated_hashes = compute_identity_hashes(mutated)

    assert mutated_hashes["params_hash"] != base_hashes["params_hash"]
    for name in IDENTITY_COMPONENTS:
        if name == "params":
            continue
        assert mutated_hashes[f"{name}_hash"] == base_hashes[f"{name}_hash"]


# --------------------------------------------------------------------------- #
# Cross-type collision resistance (the review blocker)                         #
# --------------------------------------------------------------------------- #


def test_decimal_never_collides_with_its_prefix_string() -> None:
    assert canonical_sha256(Decimal("1.0")) != canonical_sha256("__decimal__:1.0")


def test_datetime_and_date_never_collide_with_iso_strings() -> None:
    dt = datetime(2026, 1, 1, tzinfo=UTC)
    d = date(2026, 1, 2)
    assert canonical_sha256(dt) != canonical_sha256(dt.isoformat())
    assert canonical_sha256(dt) != canonical_sha256(f"__datetime__:{dt.isoformat()}")
    assert canonical_sha256(d) != canonical_sha256(d.isoformat())
    # A date and a datetime are also distinct from each other.
    assert canonical_sha256(d) != canonical_sha256(dt)


def test_list_tuple_and_set_are_distinct_containers() -> None:
    assert canonical_sha256([1, 2]) != canonical_sha256((1, 2))
    assert canonical_sha256([1, 2]) != canonical_sha256({1, 2})
    assert canonical_sha256((1, 2)) != canonical_sha256({1, 2})


def test_forged_tag_shaped_input_cannot_impersonate_special_nodes() -> None:
    # A raw list/dict/string that mimics a tag shape is re-wrapped, so it can
    # never equal the special node it imitates.
    assert canonical_sha256(["decimal", "1.0"]) != canonical_sha256(Decimal("1.0"))
    assert canonical_sha256(["str", "x"]) != canonical_sha256("x")
    assert canonical_sha256(
        {"__type__": "decimal", "value": "1.0"}
    ) != canonical_sha256(Decimal("1.0"))


# --------------------------------------------------------------------------- #
# JSON-safety / fail-closed rules                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_float_is_rejected(bad: float) -> None:
    with pytest.raises((ValueError, TypeError)):
        encode_canonical({"x": bad})
    with pytest.raises((ValueError, TypeError)):
        canonical_json({"x": bad})


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_non_finite_decimal_is_rejected(text: str) -> None:
    with pytest.raises((ValueError, TypeError)):
        encode_canonical({"x": Decimal(text)})


def test_finite_decimal_still_roundtrips() -> None:
    assert encode_canonical(Decimal("-0.0001")) == ["decimal", "-0.0001"]


@pytest.mark.parametrize(
    "value",
    [-0.0, 0.0, 1e20, 5e-324, 1.7976931348623157e308, 0.1, -0.1],
    ids=["neg0", "pos0", "e20", "min-subnormal", "max-finite", "0.1", "-0.1"],
)
def test_float_encodes_as_exact_stable_hex_string(value: float) -> None:
    encoded = encode_canonical(value)
    assert encoded == ["float", value.hex()]
    # Payload is a string (JSONB-stable), not a JSON number.
    assert isinstance(encoded[1], str)
    # A JSON round-trip (what JSONB does) reproduces the identical digest.
    roundtripped = json.loads(json.dumps(encoded))
    assert hash_canonical_ast(roundtripped) == canonical_sha256(value)


def test_signed_zero_floats_are_distinguished() -> None:
    assert canonical_sha256(-0.0) != canonical_sha256(0.0)


def test_int_and_string_key_do_not_collide() -> None:
    with pytest.raises((TypeError, ValueError)):
        encode_canonical({1: "int", "1": "str"})


def test_non_string_key_rejected_even_when_nested() -> None:
    with pytest.raises((TypeError, ValueError)):
        encode_canonical({"outer": {2: "deep"}})
    with pytest.raises((TypeError, ValueError)):
        encode_canonical({"outer": [{"ok": 1}, {3: "bad"}]})


def test_unsupported_type_is_rejected() -> None:
    with pytest.raises((TypeError, ValueError)):
        encode_canonical({"blob": b"bytes"})


def test_heterogeneous_set_is_deterministic_not_a_typeerror() -> None:
    payload = {1, "1", "a", 2}
    first = encode_canonical(payload)
    assert first[0] == "set"
    assert first == encode_canonical({2, "a", "1", 1})  # order-independent input
    assert canonical_sha256(payload) == canonical_sha256({2, "a", "1", 1})
    # 1 and "1" are now distinguished (typed), not merged.
    assert canonical_sha256({1}) != canonical_sha256({"1"})


def test_formerly_colliding_set_members_are_now_distinguished() -> None:
    # Decimal("1.0") and the hand-crafted string that mimicked its old encoding
    # are now distinct typed nodes, so the set is unambiguous (no fail-close).
    encoded = encode_canonical({Decimal("1.0"), "__decimal__:1.0"})
    assert encoded[0] == "set"
    assert len(encoded[1]) == 2
    assert ["decimal", "1.0"] in encoded[1]
    assert ["str", "__decimal__:1.0"] in encoded[1]


# --------------------------------------------------------------------------- #
# AST hashing entry point (persisted manifest, no double-encode)              #
# --------------------------------------------------------------------------- #


def test_ast_manifest_rehashes_to_same_component_hashes() -> None:
    components = _identity_components()
    raw_hashes = compute_identity_hashes(components)
    manifest = encode_manifest(components)
    # Simulate a JSONB round-trip (tuple->list normalisation, etc.).
    roundtripped = json.loads(json.dumps(manifest))
    ast_hashes = compute_identity_hashes_from_ast(roundtripped)
    assert ast_hashes == raw_hashes


def test_hash_canonical_ast_does_not_re_encode() -> None:
    # Hashing an already-encoded node must not wrap it again.
    ast = encode_canonical({"roi": Decimal("0.05")})
    assert hash_canonical_ast(ast) == canonical_sha256({"roi": Decimal("0.05")})
    # Double-encoding would change the digest.
    assert hash_canonical_ast(encode_canonical(ast)) != hash_canonical_ast(ast)


def test_derive_experiment_id_is_deterministic_and_identity_sensitive() -> None:
    hashes = compute_identity_hashes(_identity_components())
    exp_id = derive_experiment_id("NFIX", "v1", hashes)
    assert len(exp_id) == _HEX64
    assert exp_id == derive_experiment_id("NFIX", "v1", hashes)
    assert exp_id != derive_experiment_id("NFIX", "v2", hashes)

    mutated = _identity_components()
    mutated["code"] = "def populate_entry_trend(): return 1"
    assert exp_id != derive_experiment_id(
        "NFIX", "v1", compute_identity_hashes(mutated)
    )


def test_typed_canonical_ast_and_experiment_id_golden_bytes() -> None:
    """Moving the authority must not rewrite any ROB-846 persisted identity."""
    payload = {
        "alpha": Decimal("1.30"),
        "when": datetime(2026, 1, 1, tzinfo=UTC),
        "kinds": ("x", 2),
        "config": {"fee": 0.0004},
    }
    expected_ast = (
        '["dict",[["alpha",["decimal","1.30"]],'
        '["config",["dict",[["fee",["float","0x1.a36e2eb1c432dp-12"]]]]],'
        '["kinds",["tuple",[["str","x"],["int",2]]]],'
        '["when",["datetime","2026-01-01T00:00:00+00:00"]]]]'
    )
    assert canonical_json(payload) == expected_ast
    assert canonical_sha256(payload) == (
        "744bd5ac919e9577150f5cc639167a6f7d66bb0cd29edbc08d2f8c50041ae5b2"
    )

    components = {name: {"component": name, "value": 1} for name in IDENTITY_COMPONENTS}
    hashes = compute_identity_hashes(components)
    assert derive_experiment_id("ROB-847-golden", "v1", hashes) == (
        "3073fdcbb7d8e0ca515ed667e1b6d452244d07009000c9ed02d1789924699460"
    )

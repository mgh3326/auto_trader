"""ROB-846 — canonical SHA-256 identity helpers (unit).

The experiment registry pins strategy/code/params/dataset/PIT/frozen-config/
policy/benchmark/cost/MDD to a canonical, order-independent SHA-256 identity so
a strategy version can be reproduced exactly. These tests lock the canonical
form down.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.research_canonical_hash import (
    IDENTITY_COMPONENTS,
    canonical_json,
    canonical_sha256,
    compute_identity_hashes,
    derive_experiment_id,
    to_jsonable,
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


def test_canonical_json_uses_compact_separators_and_sorted_keys() -> None:
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_distinct_payloads_hash_differently() -> None:
    assert canonical_sha256({"roi": 0.05}) != canonical_sha256({"roi": 0.06})


def test_decimal_and_datetime_are_hashed_deterministically() -> None:
    payload = {"pf": Decimal("1.30"), "at": datetime(2026, 1, 1, tzinfo=UTC)}
    assert canonical_sha256(payload) == canonical_sha256(payload)
    # Decimal serializes as canonical string, not float, so 1.30 != 1.3 text.
    assert "1.30" in canonical_json(payload)


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
    # Only the mutated component's hash changes.
    for name in IDENTITY_COMPONENTS:
        if name == "params":
            continue
        assert mutated_hashes[f"{name}_hash"] == base_hashes[f"{name}_hash"]


def test_to_jsonable_is_json_safe_and_hash_consistent() -> None:
    from datetime import date

    payload = {
        "pf": Decimal("1.30"),
        "at": datetime(2026, 1, 1, tzinfo=UTC),
        "on": date(2026, 1, 2),
        "tags": {"b", "a", "c"},
        "nested": {"levels": [Decimal("2.5"), {"deep": Decimal("0.10")}]},
    }
    jsonable = to_jsonable(payload)
    # Must serialise to JSON (what JSONB storage requires).
    reparsed = json.loads(json.dumps(jsonable))
    # Hashing the raw payload, the json-safe form, and the DB roundtrip all match.
    assert canonical_sha256(payload) == canonical_sha256(jsonable)
    assert canonical_sha256(payload) == canonical_sha256(reparsed)
    # Decimal is preserved losslessly as canonical text, not a float.
    assert jsonable["pf"] == "__decimal__:1.30"
    assert jsonable["tags"] == ["a", "b", "c"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_float_is_rejected(bad: float) -> None:
    with pytest.raises((ValueError, TypeError)):
        to_jsonable({"x": bad})
    with pytest.raises((ValueError, TypeError)):
        canonical_json({"x": bad})


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_non_finite_decimal_is_rejected(text: str) -> None:
    bad = Decimal(text)
    with pytest.raises((ValueError, TypeError)):
        to_jsonable({"x": bad})


def test_finite_decimal_still_roundtrips() -> None:
    assert to_jsonable(Decimal("-0.0001")) == "__decimal__:-0.0001"


def test_int_and_string_key_do_not_collide() -> None:
    # {1: ..., "1": ...} must not silently collapse to a single "1" key.
    with pytest.raises((TypeError, ValueError)):
        to_jsonable({1: "int", "1": "str"})


def test_non_string_key_rejected_even_when_nested() -> None:
    with pytest.raises((TypeError, ValueError)):
        to_jsonable({"outer": {2: "deep"}})
    with pytest.raises((TypeError, ValueError)):
        to_jsonable({"outer": [{"ok": 1}, {3: "bad"}]})


def test_heterogeneous_set_is_deterministic_not_a_typeerror() -> None:
    # Previously sorting {1, "1"} raised TypeError; now it is deterministic.
    payload = {1, "1", "a", 2}
    first = to_jsonable(payload)
    assert isinstance(first, list)
    assert first == to_jsonable({2, "a", "1", 1})  # order-independent input
    assert canonical_sha256(payload) == canonical_sha256({2, "a", "1", 1})


def test_set_members_that_collide_to_same_canonical_form_fail_close() -> None:
    # A raw Decimal and a hand-crafted string that mimics its encoded form must
    # not silently map to the same canonical member.
    with pytest.raises((ValueError, TypeError)):
        to_jsonable({Decimal("1.0"), "__decimal__:1.0"})


def test_derive_experiment_id_is_deterministic_and_identity_sensitive() -> None:
    hashes = compute_identity_hashes(_identity_components())
    exp_id = derive_experiment_id("NFIX", "v1", hashes)
    assert len(exp_id) == _HEX64
    assert exp_id == derive_experiment_id("NFIX", "v1", hashes)

    # A different version is a different identity.
    assert exp_id != derive_experiment_id("NFIX", "v2", hashes)

    # A different code hash (same version) is a different identity.
    mutated = _identity_components()
    mutated["code"] = "def populate_entry_trend(): return 1"
    assert exp_id != derive_experiment_id(
        "NFIX", "v1", compute_identity_hashes(mutated)
    )

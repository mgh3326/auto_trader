"""ROB-945 (H5) -- canonical payload converter RED tests.

``to_canonical_payload`` must turn a tree of frozen dataclasses (H4's
``SignalEvent``/``TradeRecord``/``WalkForwardResult`` etc.) into a plain
JSON-native structure that ``research_contracts.canonical_hash.canonical_sha256``
can hash directly -- ``encode_canonical`` itself raises ``TypeError`` on a
raw dataclass instance, so this conversion step is required before any H5
hash can be computed over an H4 DTO tree.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from rob945_canonical_payload import to_canonical_payload

from research_contracts.canonical_hash import canonical_sha256


@dataclass(frozen=True)
class _Leaf:
    a: int
    b: str


@dataclass(frozen=True)
class _Nested:
    leaf: _Leaf
    items: tuple[_Leaf, ...]
    mapping: dict[str, int]


def test_plain_scalars_pass_through_unchanged():
    assert to_canonical_payload(1) == 1
    assert to_canonical_payload("x") == "x"
    assert to_canonical_payload(True) is True
    assert to_canonical_payload(None) is None
    assert to_canonical_payload(1.5) == 1.5


def test_dataclass_becomes_plain_dict_keyed_by_field_name():
    payload = to_canonical_payload(_Leaf(a=1, b="x"))
    assert payload == {"a": 1, "b": "x"}


def test_tuple_of_dataclasses_becomes_list_of_dicts():
    payload = to_canonical_payload((_Leaf(a=1, b="x"), _Leaf(a=2, b="y")))
    assert payload == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]


def test_nested_dataclass_recurses_through_every_container_kind():
    value = _Nested(
        leaf=_Leaf(a=1, b="x"),
        items=(_Leaf(a=2, b="y"),),
        mapping={"k": 3},
    )
    payload = to_canonical_payload(value)
    assert payload == {
        "leaf": {"a": 1, "b": "x"},
        "items": [{"a": 2, "b": "y"}],
        "mapping": {"k": 3},
    }


def test_result_is_hashable_by_the_canonical_authority_without_a_type_error():
    value = _Nested(leaf=_Leaf(a=1, b="x"), items=(), mapping={})
    # encode_canonical would raise TypeError on the raw dataclass directly;
    # the whole point of this converter is to make that hashable.
    digest = canonical_sha256(to_canonical_payload(value))
    assert isinstance(digest, str)
    assert len(digest) == 64


def test_two_field_orderings_of_the_same_data_hash_identically():
    """Dict key order must never leak into the hash -- the canonical
    authority already sorts keys, but this converter must not, itself,
    introduce an order-sensitive representation (e.g. a list of pairs)."""
    a = to_canonical_payload({"x": 1, "y": 2})
    b = to_canonical_payload({"y": 2, "x": 1})
    assert canonical_sha256(a) == canonical_sha256(b)


def test_non_finite_float_becomes_the_stable_sentinel_string_not_a_json_error():
    assert to_canonical_payload(float("inf")) == "nonfinite:inf"
    assert to_canonical_payload(float("-inf")) == "nonfinite:-inf"
    assert to_canonical_payload(float("nan")) == "nonfinite:nan"


def test_unsupported_type_raises_instead_of_silently_stringifying():
    class _Unsupported:
        pass

    with pytest.raises(TypeError):
        to_canonical_payload(_Unsupported())

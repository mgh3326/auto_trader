# tests/services/action_report/common/test_canonicalize.py
import datetime as dt

from app.services.action_report.common.canonicalize import canonical_payload_hash


def test_hash_is_stable_for_identical_payload():
    a = {"symbol": "035420", "price": 195000.0}
    b = {"price": 195000.0, "symbol": "035420"}
    assert canonical_payload_hash(a) == canonical_payload_hash(b)


def test_hash_strips_sub_second_timestamps_to_second_precision():
    base = dt.datetime(2026, 5, 19, 11, 11, 1, tzinfo=dt.UTC)
    near = dt.datetime(2026, 5, 19, 11, 11, 1, 999_999, tzinfo=dt.UTC)
    a = {"as_of": base.isoformat()}
    b = {"as_of": near.isoformat()}
    assert canonical_payload_hash(a) == canonical_payload_hash(b)


def test_hash_excludes_source_timestamps_block():
    base = {"data": {"price": 195000.0}}
    a = {**base, "source_timestamps": {"fetched_at": "2026-05-19T11:11:00Z"}}
    b = {**base, "source_timestamps": {"fetched_at": "2026-05-19T11:11:30Z"}}
    assert canonical_payload_hash(a) == canonical_payload_hash(b)


def test_hash_normalizes_float_to_nine_digit_precision():
    a = {"price": 1.123456789012}
    b = {"price": 1.123456789}
    assert canonical_payload_hash(a) == canonical_payload_hash(b)


def test_hash_differs_when_meaningful_field_differs():
    a = {"symbol": "035420", "price": 195000.0}
    b = {"symbol": "035420", "price": 195100.0}
    assert canonical_payload_hash(a) != canonical_payload_hash(b)


def test_hash_is_64_char_sha256_hex():
    h = canonical_payload_hash({"x": 1})
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_hash_handles_nested_dicts_and_lists():
    a = {"items": [{"k": 1}, {"k": 2}]}
    b = {"items": [{"k": 1}, {"k": 2}]}  # same — list order preserved (not sorted)
    assert canonical_payload_hash(a) == canonical_payload_hash(b)


def test_hash_list_order_is_significant():
    a = {"items": [1, 2, 3]}
    b = {"items": [3, 2, 1]}
    assert canonical_payload_hash(a) != canonical_payload_hash(b)


def test_hash_treats_none_and_missing_as_different():
    # Conservative: explicit null is meaningful information vs absent key.
    a = {"symbol": "035420", "name": None}
    b = {"symbol": "035420"}
    assert canonical_payload_hash(a) != canonical_payload_hash(b)

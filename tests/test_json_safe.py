from __future__ import annotations

import json
import math

from app.core.json_safe import sanitize_non_finite


def test_sanitize_non_finite_recursive_and_strict_json_safe():
    raw = {
        "pf": float("inf"),
        "neg": float("-inf"),
        "nan": float("nan"),
        "finite": 1.5,
        "ints": [1, float("inf"), 3],
        "nested": {"a": float("nan"), "b": "ok", "c": 0.0},
        "flag": True,
        "text": "Infinity is a string here",
    }
    out = sanitize_non_finite(raw)
    assert out["pf"] is None
    assert out["neg"] is None
    assert out["nan"] is None
    assert out["finite"] == 1.5
    assert out["ints"] == [1, None, 3]
    assert out["nested"] == {"a": None, "b": "ok", "c": 0.0}
    assert out["flag"] is True  # bool not coerced
    assert out["text"] == "Infinity is a string here"
    json.dumps(out, allow_nan=False)  # raises if any non-finite remains


def test_sanitize_non_finite_does_not_mutate_input():
    raw = {"pf": float("inf")}
    sanitize_non_finite(raw)
    assert math.isinf(raw["pf"])


def test_validated_run_card_reexports_sanitize_non_finite():
    from app.schemas.validated_run_card import sanitize_non_finite as reexported

    assert reexported is sanitize_non_finite

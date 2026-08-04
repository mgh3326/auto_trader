"""Frozen v2 semantics, including the code that enforces them."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

CONTRACT_VERSION = "intraday-harness-v2.0.0"
_ROOT = Path(__file__).resolve().parent
_SELF_HASH_PLACEHOLDER = "__SELF_SHA256_PLACEHOLDER__"
_SELF_HASH_EXPECTED = "70f20c166673304a01a5e21ef1faa07b533b65e46f1ae265d0a591bdc2c8db49"

# These pins deliberately cover executable enforcement, not just this text.
_ENFORCEMENT_SHA256 = {
    "engine.py": "e2e89fc2fd5f41bde9a473691c06743dbfd4eb56f6a5539296f5bd0ca5a342a3",
    "__init__.py": "0b8f73cd56a274f84fa2da3cea64dd2b4cef0bea1c22165af166a22a481b62f2",
    "contract.py": _SELF_HASH_EXPECTED,
}
_DECLARATION = {
    "version": CONTRACT_VERSION,
    "supported": {
        "signal": "bar-close",
        "fill": "strictly-next-bar-open",
        "order_type": "market",
        "fill_policy": "all-or-none",
        "missing_bar": "INCOMPLETE",
        "incomplete_bar": "INCOMPLETE",
        "costs": ["fee", "slippage"],
    },
    "excluded": ["limit", "partial", "stop-target-order", "extended-hours"],
    "forbidden": ["forward-fill", "same-bar-fill", "randomness", "wall-clock"],
    "enforcement_sha256": _ENFORCEMENT_SHA256,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _actual_enforcement_hashes() -> dict[str, str]:
    actual: dict[str, str] = {}
    for name in _ENFORCEMENT_SHA256:
        path = _ROOT / name
        if name == "contract.py":
            source = path.read_bytes()
            marker = _SELF_HASH_PLACEHOLDER.encode()
            expected = _SELF_HASH_EXPECTED.encode()
            if source.count(expected) != 1 or source.count(marker) != 1:
                raise RuntimeError(
                    "contract.py must contain exactly one self-hash value and marker"
                )
            normalized = source.replace(expected, marker)
            actual[name] = hashlib.sha256(normalized).hexdigest()
        else:
            actual[name] = _sha256(path)
    return actual


def verify_contract() -> None:
    actual = _actual_enforcement_hashes()
    if actual != _ENFORCEMENT_SHA256:
        raise RuntimeError(
            "intraday harness enforcement source changed; freeze pin must be updated "
            f"(expected={_ENFORCEMENT_SHA256!r}, actual={actual!r})"
        )


verify_contract()
CONTRACT_HASH = hashlib.sha256(_canonical_json(_DECLARATION)).hexdigest()

__all__ = ["CONTRACT_HASH", "CONTRACT_VERSION", "verify_contract"]

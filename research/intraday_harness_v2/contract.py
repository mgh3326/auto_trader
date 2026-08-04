"""Frozen v2 semantics, including the code that enforces them."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

CONTRACT_VERSION = "intraday-harness-v2.0.0"
_ROOT = Path(__file__).resolve().parent

# These pins deliberately cover executable enforcement, not just this text.
_ENFORCEMENT_SHA256 = {
    "engine.py": "77fa524e09a78e07d71057983b3f2be50f3904824109f197a954ee27103b1a70",
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
    return {name: _sha256(_ROOT / name) for name in _ENFORCEMENT_SHA256}


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

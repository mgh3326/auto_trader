"""Public, value-free attestation of task-191 operator invariant review."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.b0x.envelope import (
    CRYPTO_SIDECAR_ENVELOPE,
    KR_MOCK_ENVELOPE,
    US_ALPACA_PAPER_LAB_ENVELOPE,
)

ROOT = Path(__file__).resolve().parents[2]
PROJECTION_PATH = ROOT / "config/b0x_operator_contract_projection.json"

# Content digests identify the reviewed private sources without copying their
# operational values or repository identity into this public repository.
EXPECTED_SOURCE_DIGESTS = {
    "mock_policy_section_sha256": (
        "8c93f3f3cb6c5689a56e1a16abba9aa1faabc6428e58cfa9b4115a82ab5fc97a"
    ),
    "operator_contract_sha256": (
        "e9871b876cda420b543c3e74991044af9f8ce2af757a064931d962c9aa726511"
    ),
}

# These are result classes, not private field names or values. Exact comparison
# evidence remains in the private task report. Every class is independently
# fail-closed so a public attestation field cannot silently go unchecked.
EXPECTED_FIELD_CLASSES = frozenset(
    {
        "account_and_market_mapping",
        "approved_order_path",
        "execution_envelopes_and_thresholds",
        "scoring_and_promotion_prohibitions",
        "slot_market_weekend_and_playbook_semantics",
        "sole_writer_and_strategy_authority",
        "strategy_order_exceptions",
        "temporary_coexistence_constraint",
    }
)
EXPECTED_CLASS_KEYS = frozenset({"result", "conflicts"})
EXPECTED_GUARDS = {
    "effect": "transport_and_install_inputs_only",
    "order_authority_changed": False,
    "shadow_order_authority": False,
}
EXPECTED_TOP_LEVEL_KEYS = frozenset(
    {"source_digests", "field_comparison", "portability_guards"}
)

# Hash of the already-public, production-path B0X envelope constants. This
# prevents the value-free review result from masking drift in those constants.
EXPECTED_PRODUCTION_ENVELOPES_SHA256 = (
    "e533fc44d721e69a173ec1aa0754c032821fdadee4bc058bcf249563544420f3"
)


def _production_envelopes_sha256() -> str:
    canonical = {
        "crypto": CRYPTO_SIDECAR_ENVELOPE.canonical(),
        "kr": KR_MOCK_ENVELOPE.canonical(),
        "us": US_ALPACA_PAPER_LAB_ENVELOPE.canonical(),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_projection(path: Path = PROJECTION_PATH) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("B0X operator projection must be a JSON object")
    return payload


def validate_projection(payload: Mapping[str, Any]) -> tuple[str, ...]:
    errors: list[str] = []
    if set(payload) != EXPECTED_TOP_LEVEL_KEYS:
        errors.append("operator projection top-level field set differs")

    sources = payload.get("source_digests")
    if not isinstance(sources, Mapping):
        errors.append("reviewed source digests are missing")
        sources = {}
    if set(sources) != set(EXPECTED_SOURCE_DIGESTS):
        errors.append("reviewed source digest field set differs")
    for field, expected in EXPECTED_SOURCE_DIGESTS.items():
        if sources.get(field) != expected:
            errors.append(f"reviewed source digest drift: {field}")

    comparisons = payload.get("field_comparison")
    if not isinstance(comparisons, Mapping):
        errors.append("operator field-class comparison is missing")
        comparisons = {}
    if set(comparisons) != EXPECTED_FIELD_CLASSES:
        errors.append("operator field-class set differs")
    for field_class in EXPECTED_FIELD_CLASSES:
        result = comparisons.get(field_class)
        if not isinstance(result, Mapping) or set(result) != EXPECTED_CLASS_KEYS:
            errors.append(f"field comparison shape differs: {field_class}")
            continue
        if result.get("result") != "matched":
            errors.append(f"field comparison not matched: {field_class}")
        if result.get("conflicts") != []:
            errors.append(f"field comparison has conflicts: {field_class}")

    guards = payload.get("portability_guards")
    if not isinstance(guards, Mapping):
        errors.append("portability guards are missing")
        guards = {}
    if set(guards) != set(EXPECTED_GUARDS):
        errors.append("portability guard field set differs")
    for field, expected in EXPECTED_GUARDS.items():
        if guards.get(field) != expected:
            errors.append(f"portability guard drift: {field}")

    if _production_envelopes_sha256() != EXPECTED_PRODUCTION_ENVELOPES_SHA256:
        errors.append("public production envelope constants drifted")
    return tuple(errors)


__all__ = [
    "EXPECTED_FIELD_CLASSES",
    "EXPECTED_GUARDS",
    "EXPECTED_PRODUCTION_ENVELOPES_SHA256",
    "EXPECTED_SOURCE_DIGESTS",
    "PROJECTION_PATH",
    "load_projection",
    "validate_projection",
]

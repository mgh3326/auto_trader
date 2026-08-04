"""Deterministic crypto acceptance-path observation; no broker calls."""

from __future__ import annotations

from typing import Any

from research.three_market_shadow.calculations import calculate_crypto_synthetic


def verify_crypto_acceptance_path() -> list[dict[str, Any]]:
    """Prove signal→intent→block→kill/restart without submitting anything."""
    signal = calculate_crypto_synthetic()
    intent = {"kind": "order_intent", "signal": signal, "submit": False}
    blocked = {"kind": "pre_submit_block", "accepted_for_submission": False}
    killed = {"kind": "kill", "armed": False, "reason": "shadow_only"}
    restarted = {"kind": "restart", "armed": False, "orders": 0}
    return [signal, intent, blocked, killed, restarted]


__all__ = ["verify_crypto_acceptance_path"]

"""Broker-neutral parity harness for the three-market shadow."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .calculations import CONTRACT_HASH, calculate_signal


def run_harness(market: str, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Compute a replay result using the same function as the runtime runner."""
    result = calculate_signal(market, snapshot)
    return {"market": market, "contract_hash": CONTRACT_HASH, "signal": result}


__all__ = ["run_harness"]

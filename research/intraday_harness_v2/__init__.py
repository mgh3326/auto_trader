"""Minimum deterministic intraday execution harness (v2 contract)."""

from .contract import CONTRACT_HASH, CONTRACT_VERSION, verify_contract
from .engine import (
    Bar,
    BarSeries,
    ExecutionSummary,
    Fill,
    IncompleteReason,
    Side,
    Signal,
    run,
)

verify_contract()

__all__ = [
    "Bar",
    "BarSeries",
    "CONTRACT_HASH",
    "CONTRACT_VERSION",
    "ExecutionSummary",
    "Fill",
    "IncompleteReason",
    "Signal",
    "Side",
    "run",
]

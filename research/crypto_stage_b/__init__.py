"""Crypto Stage-B research engine, isolated from runtime and KR strategy code."""

from .contracts import CryptoStageBRunContract, VenueCostLiteral
from .engine import CandidatePairResult, run_candidate_pair, run_execution_arm
from .registry import CandidateRegistry
from .report import HarnessReport, build_harness_report
from .source import DailyBar, InMemoryDailyBarSource, TerminalEvent

__all__ = [
    "CandidatePairResult",
    "CandidateRegistry",
    "CryptoStageBRunContract",
    "DailyBar",
    "HarnessReport",
    "InMemoryDailyBarSource",
    "TerminalEvent",
    "VenueCostLiteral",
    "build_harness_report",
    "run_candidate_pair",
    "run_execution_arm",
]

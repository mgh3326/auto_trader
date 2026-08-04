"""KR backtest harness — Stage A wiring plus a bounded Stage-B bridge.

JOB_PURPOSE=BACKTEST_HARNESS_WIRING_ONLY

This package wires loaders, PIT membership, holdout refusal, a pipeline-smoke
baseline, and an explicit-contract Stage-B bridge. The Stage-B result remains
unpromoted research evidence.

Schema contracts are **inferred from kr-corpus-v1 brief §3 literals** and
must be loud-failed against a real terminal manifest when Stage B opens.
"""

from __future__ import annotations

JOB_PURPOSE = "BACKTEST_HARNESS_WIRING_ONLY"
PIPELINE_SMOKE_LABEL = "PIPELINE_SMOKE_NOT_A_STRATEGY"
BASELINE_SMOKE_NAME = "liquidity_proxy_decile_topN_D5"

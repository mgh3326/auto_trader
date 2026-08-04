"""KR backtest harness — Stage A wiring (fixture only).

JOB_PURPOSE=BACKTEST_HARNESS_WIRING_ONLY

This package wires loaders, PIT membership, holdout refusal, and a
pipeline-smoke baseline. It is **not** strategy research.

Schema contracts are **inferred from kr-corpus-v1 brief §3 literals** and
must be loud-failed against a real terminal manifest when Stage B opens.
"""

from __future__ import annotations

JOB_PURPOSE = "BACKTEST_HARNESS_WIRING_ONLY"
PIPELINE_SMOKE_LABEL = "PIPELINE_SMOKE_NOT_A_STRATEGY"
BASELINE_SMOKE_NAME = "liquidity_proxy_decile_topN_D5"

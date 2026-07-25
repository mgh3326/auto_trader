"""ROB-993 — Binance Demo live execution loop, strategy-pluggable.

Real-time 1m->4h bar aggregation (reusing the H1 offline builder's
complete-only / UTC-aligned / no-forward-fill semantics), a plugin
strategy interface (``evaluate(bars_4h_multi_symbol) -> Signal | None``),
a kill switch (max concurrent positions + consecutive stop-loss stop), and
wiring into the existing ``BinanceFuturesDemoExecutionClient`` (ROB-298 —
1x leverage, reduceOnly close, demo-fapi.binance.com only).

Strategy-agnostic infrastructure only. The S3 signal-engine adapter
(ROB-980/rob974_h3_s3) is deliberately NOT wired here — see
``docs/runbooks/binance-demo-strategy-loop.md``. No scheduler/TaskIQ
registration; the only entry point is
``scripts/binance_demo_strategy_loop.py``, run manually by an operator.
"""

from __future__ import annotations

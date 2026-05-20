"""ROB-286 — Deterministic scalping decision + runner package.

The scalper is the consumer side of the testnet execution adapter:

  * ``decision.compute_action`` is a pure function (no I/O, no DB) that
    decides whether to Hold / Entry / Exit given a market snapshot and
    the per-symbol state.
  * ``runner.ScalperRunner`` orchestrates the loop: read market data
    (Child B), compute action, call execution adapter (this PR), record
    ledger transition.
  * ``config`` pins the MVP symbol set, max notional, and the
    indicator thresholds used by the decision function.

No LLM, no Hermes/Discord approval, no scheduler. CLI-only invocation
via ``scripts/binance_testnet_scalper_smoke.py`` (Task 13).
"""

from __future__ import annotations

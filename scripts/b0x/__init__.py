"""B0-X — 「내 규칙을 기계가 그대로 실행하면 어떻게 되는가」 관측 어댑터.

Contract of record: ``~/work/herdr-inbox/b0x-experiment-contract-v1-20260808.md``
(operator-signed 2026-08-08). Account map of record:
``~/services/auto_trader-operator/mock/CLAUDE.md`` §1 + ``strategy_order_exceptions``.

This package is **not** a strategy and produces **no** promotion evidence.
It reads a ``policy_table.v1`` artifact (built read-only by
``scripts/build_policy_table.py``, which this package never modifies or
invokes) and derives orders from its rows **deterministically** — no LLM, no
session judgment, no clock-dependent branching in the order content.

Layering (X-K / X-U succeed the market-agnostic half unchanged):

  * ``labels`` / ``envelope`` / ``table_source`` / ``state`` / ``derivation``
    / ``kill_switch`` / ``ledger`` — market-agnostic core.
  * ``crypto.shadow`` — Upbit shadow-sim (본선): synthetic fills, zero real
    orders on any venue.
  * ``crypto.sidecar`` — Binance Spot Demo (사이드카): BTC/ETH/SOL-USDT only,
    behind a per-call ``confirm`` operator gate inherited from ROB-298.

Hard boundaries enforced by code + tests in ``tests/scripts/b0x/``:
  * zero live-broker imports, zero in-process LLM provider imports;
  * the §4 envelope constants cannot be widened by CLI flag or env var;
  * a missing/STALE/hash-mismatched table yields zero orders, never a
    silent reuse or recomputation.
"""

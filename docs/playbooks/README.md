# Playbooks

Public-safe **procedure contracts**: descriptions of how the live trading
session actually sequences MCP tools, which gates apply, and which policy keys
govern thresholds. They exist to make analysis reproducible across sessions and
models — the first step toward moving the decision frame out of prompt/context
and into MCP-side tools and policy.

## Documents

- [`trading-decision-playbook.md`](trading-decision-playbook.md) — as-is
  baseline (2026-06-19 → 2026-07-02) of the buy / sell / new-idea-discovery
  pipelines. Hybrid format: human prose (§0–§5) + machine-readable `lanes:` and
  `policy_keys:` blocks. The `lanes:` blocks are the lane-definition source for
  ROB-649 (`route_request`); the `policy_keys:` block is the initial-value
  capture source for ROB-646 (trading policy YAML).

## Boundary

These are **procedure contracts, not operator instructions.** They describe the
_shape_ of the decision flow only. They contain no account numbers, balances,
asset size, credentials, or routing secrets — see the
[report-workflows boundary](../invest/report-workflows/README.md#procedure-contract-vs-operator-instruction)
for the full rule. Threshold numbers are captured once in the `policy_keys:` block
(accepted public exposure; ROB-646 YAML becomes authoritative once it lands).

## Drift control

`tests/test_playbook_tool_names.py` parses the `lanes:` blocks and fails if any
referenced tool no longer exists in the DEFAULT MCP profile, keeping these
documents honest against the live tool registry.

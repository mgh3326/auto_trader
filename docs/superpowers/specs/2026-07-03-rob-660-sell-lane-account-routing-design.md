# ROB-660 — sell lane account-routing fix (route_request)

**Date:** 2026-07-03
**Linear:** ROB-660 (High) — `route_request(profit_taking)` lane blocks `kis_live_place_order` / `toss_cancel_order`
**Migration:** 0 (no DB change)

## Problem

`route_request(intent="profit_taking", market="kr")` puts two tools in
`blocked_actions` that lane-compliant profit-taking actually needs:

- **`kis_live_place_order`** — most real profit-taking targets are **KIS holdings**
  (삼전, 한화에어로, KAI/휴젤 …). The hard rule is "sells execute from the holding
  account", but the sell lane only lists `toss_place_order`, so selling KIS holdings
  is impossible inside the lane.
- **`toss_cancel_order`** — Toss forbids two-sided (buy+sell) resting orders on the
  same symbol, so a name with a pending buy limit can only be sold after the buy is
  cancelled. With cancel blocked, the two-sided state cannot be resolved in-lane
  (real case: 6/30 네오위즈 lock refused because of a pending buy).

Advisory-only today (execution is not enforced), but a future enforcement
middleware (ROB-469 follow-up) would make this lane definition break live
operation. Fix must land before enforcement.

The buy lane already lists both `toss_place_order` **and** `kis_live_place_order`
(playbook §1), so its only issue-flagged gap ("KIS 예수금 매수 시
`kis_live_place_order` 필요") is already satisfied — **buy lane is out of scope.**

## Design

Scope: **sell lane only.** All changes are additive; no behaviour changes for buy,
bootstrap, discovery, or any crypto/US path.

### 1. `LANE_SEQUENCES["sell"]` — two new ordered steps

Mirrors the buy lane's dual-place symmetry (Toss + KIS place tools both listed):

| # | tool | purpose |
|---|------|---------|
| 1 | `toss_get_positions` | scan ITM / near-breakeven names |
| 2 | `analyze_stock_batch` | confirm distance to resistance, RSI, upside |
| 3 | **`toss_cancel_order`** *(new)* | clear same-symbol buy pending first (Toss two-sided constraint) |
| 4 | `toss_place_order` | sell-into-strength split ladder — Toss holdings |
| 5 | **`kis_live_place_order`** *(new)* | sell KIS holdings from the holding account; `dry_run` preview → live |
| 6 | `sell_ladder_fill_preview` | ROB-477 bottom-anchor rung, fill-safety |

Both new tools are in `MUTATION_TOOLS`; adding them to the sequence puts them into
the lane's `allowed_tools` (via `lane_tool_names` ∩ `MUTATION_TOOLS` →
`lane_own_mutation`) and out of `blocked_actions`. They surface in
`standard_tool_sequence`.

### 2. `LANE_EXTRA_ALLOWED` — allowed-only helpers (new)

```python
LANE_EXTRA_ALLOWED: dict[str, frozenset[str]] = {
    "sell": frozenset({"kis_live_get_order_history", "toss_get_order_history"}),
}
```

The two order-history tools are read-only in reality but are bucketed in
`MUTATION_TOOLS` for registry partitioning, so they are otherwise blocked. They are
liveness / fill-status confirmation helpers, not ordered workflow steps — surfacing
them as sequence steps would be noise. `LANE_EXTRA_ALLOWED` un-blocks them
(allowed-only) without touching the ordered sequence or the playbook YAML. Pattern
parallels ROB-658's `MARKET_EXECUTION_TOOLS` supplement.

`build_route_plan` integration:

```python
lane_extra = LANE_EXTRA_ALLOWED.get(lane, frozenset())
allowed = (lane_tools | market_exec | lane_preview | lane_extra
           | set(READ_ONLY_ADVISORY_TOOLS)) & registered_tools
blocked = (MUTATION_TOOLS - lane_own_mutation - lane_preview - lane_extra) & registered_tools
```

Add `LANE_EXTRA_ALLOWED` to `__all__`.

### 3. `HARD_CONSTRAINTS["sell"]` — two new lines

- `sell from holding account: Toss holdings -> toss_place_order, KIS holdings -> kis_live_place_order`
- `same-symbol buy pending on Toss -> toss_cancel_order first (two-sided constraint)`

Makes the lane self-documenting for the future enforcement middleware. These are
prose summaries (not policy-key references), consistent with existing
`HARD_CONSTRAINTS` entries; `HARD_CONSTRAINTS` is not sync-tested against the
playbook `gates:` block.

### 4. `docs/playbooks/trading-decision-playbook.md`

- **Sell lane YAML block** (`## 2)` §, `lanes: sell: steps:`): add `toss_cancel_order`
  and `kis_live_place_order` `tool:` entries in the new order. **Required** —
  `test_lane_sequences_match_playbook` asserts exact set-equality between
  `lane_tool_names("sell")` and the playbook `tool:` refs. History tools are NOT
  added (allowed-only, not sequenced).
- **Sell prose (§2)**: update the execute step to describe the KIS-holdings sell path
  and the cancel-first precondition; add a short "allowed helpers (not sequenced):
  `*_get_order_history`" note in the ROB-658 divergence-note style so prose matches
  code.

## Behaviour preserved

- KR buy lane, bootstrap, discovery, and all crypto/US paths unchanged.
- ROB-658 crypto/US injection still fires for sell (neither Toss nor KIS place tools
  register on those profiles → generic `place_order` injected as before). New
  sequenced tools (`toss_cancel_order`, `kis_live_place_order`) are unregistered on
  crypto/US → dropped by the profile intersection, never surfaced there.
- `toss_cancel_order` stays blocked in buy / discovery / bootstrap (no cross-lane
  leak).
- Determinism, contiguous 1..n step numbering, and the read-only/mutation partition
  (registry-diff guard) all hold.

## Tests (TDD, `tests/test_route_request_lanes.py` unless noted)

New / updated assertions:

1. **sell place+cancel**: `kis_live_place_order` and `toss_cancel_order` are in
   `allowed_tools` + `standard_tool_sequence`, not in `blocked_actions` (kr, `_ALL`).
2. **sell history helpers**: `kis_live_get_order_history` and `toss_get_order_history`
   are in `allowed_tools`, not in `blocked_actions`, and NOT in
   `standard_tool_sequence`.
3. **cross-lane non-leak**: buy lane still lists `toss_cancel_order` in
   `blocked_actions` (existing `test_blocked_actions_excludes_lanes_own_mutation_tools`
   line 78 already covers this; keep it green).
4. **hard_constraints**: sell `hard_constraints` joined text contains the
   holding-account routing phrase and the cancel-first phrase.
5. **crypto sell regression**: existing
   `test_crypto_sell_and_discovery_surface_generic_place_order` still passes; add
   that the new KR tools (`toss_cancel_order`, `kis_live_place_order`) are dropped
   when unregistered on crypto.
6. **playbook sync**: `test_lane_sequences_match_playbook` passes once both the code
   sequence and the YAML block include the 2 new tools.

`tests/test_route_request.py::test_executing_lane_surfaces_toss_preview_precursor`
already exercises `profit_taking` and is unaffected.

## Out of scope

- Buy lane changes (its issue-flagged gap is already met).
- Enforcement middleware (ROB-469 follow-up) — this fix only corrects the advisory
  lane definition so enforcement, when added, inherits a correct surface.

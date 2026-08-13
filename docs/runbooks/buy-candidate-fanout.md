# Bounded buy-candidate fan-out

`discover_buy_candidates_fanout` is a KR-only, read-only discovery tool. It
widens the screened population; it does not create a proposal, calculate an
executable order, contact a broker or account, write a database row, or register
a schedule.

## Sources and bounds

Every source is sliced to `TOP_N_PER_SOURCE = 10` before it can enter the
deduped population. The bound is deliberately the same as the fresh batch
revalidator's `TOP_N_REVALIDATION = 10` contract.

| Family | Source | Ordering / preset | Bound |
| --- | --- | --- | --- |
| RSI ordering | `screen_stocks` | `sort_by="rsi"`, ascending; `max_rsi` is omitted | 10 |
| Pullback | `screen_stocks` | `sort_by="change_rate"`, ascending | 10 |
| Turnover | `screen_stocks` | `sort_by="trade_amount"`, descending | 10 |
| Snapshot support/flow | persisted snapshots | `support_proximity`, `investor_flow_momentum`, `double_buy`, `stable_growth`, `undervalued_growth` | 10 per preset |
| Snapshot value/catalyst | persisted snapshots | `cheap_value`, `high_yield_value`, `undervalued_breakout`, `profitable_company`, `growth_expectation_toss` | 10 per preset |

Each snapshot source group contains exactly five presets, and the implementation
rejects a group with more than five. Snapshot rows are read through the existing
view-model snapshot path with a resolver that always returns `none`; it does not
perform the public snapshot tool's held-position lookup.

## Revalidation and funnel

Rows are deduped with the database-standard symbol form (`BRK-B` / `BRK/B` →
`BRK.B`) while keeping every `matched_sources` entry. Only the first ten deduped
symbols receive fresh analysis of current price, support, consensus, RSI, and
trade-restriction state. Snapshot price, support, and consensus evidence may be
at most one session stale and is input-only; it cannot establish a gate pass
until that fresh revalidation succeeds.

The recorded funnel is always in this order:

`source → base_eligibility → support_source_count → upside → rsi → anchor_band → budget`

The RSI stage is either `regular_pass` or `rsi_only_fail`. The latter remains a
classification only; this tool never turns it into an actionable result. The
anchor band is deliberately pre-tick and non-executable. A future separately
authorized consumer would need to redo tick-floor and all execution gates.

The literal policy gates remain unchanged: moderate, two-family support within
8%; honest upside of at least 40%; support discount 5–10%; final anchor distance
-15% to -5%; and the 90% / 50% budget caps. Because this tool cannot inspect
account or broker evidence, a reached `budget` stage is always `deferred` and
`actionable_count` is always zero.

Actual read-only smoke output, observed on 2026-08-13 (trimmed to the first
candidate's contract fields; it is not an order):

```json
{
  "symbol": "088980",
  "matched_sources": ["rsi", "change_rate"],
  "revalidation": {"status": "received", "scope": ["current_price", "supports", "consensus", "trading_restriction", "rsi_14"]},
  "funnel": {
    "source": {"status": "pass"},
    "base_eligibility": {"status": "fail", "reason": "fresh_revalidation_data_state_not_fresh"},
    "support_source_count": {"status": "not_evaluated", "reason": "base_eligibility_failed"},
    "upside": {"status": "not_evaluated", "reason": "base_eligibility_failed"},
    "rsi": {"status": "not_evaluated", "reason": "base_eligibility_failed"},
    "anchor_band": {"status": "not_evaluated", "reason": "base_eligibility_failed"},
    "budget": {"status": "not_evaluated", "reason": "base_eligibility_failed"}
  },
  "actionable": false
}
```

## 08-17 digest handoff

`digest_observation.source_stats` is the manual 08-17 digest payload. Per source
it records incoming rows, top-N clipping, deduped candidates, drop reasons,
regular-evidence candidates, RSI-only-fail candidates, and final actionable
count under `final_eligible_counts`. It is an observation only: do not use it as
PnL scoring or as immediate threshold-tuning evidence. Any later measurement or
threshold-policy change must be independently designed and operator reviewed.

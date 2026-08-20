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
symbols receive full fresh-analysis revalidation of current price, support,
consensus, RSI, trade-restriction state, and the analysis result's top-level
`data_state`. The full response is used deliberately: the shared compact KR
regular-session response can omit that aggregate freshness key. The fan-out
does not change the shared compact contract.

Snapshot price, support, and consensus evidence may be at most one session
stale and is input-only; it cannot establish a gate pass until that fresh
revalidation succeeds. The full revalidation adapter preserves all returned
support levels rather than inheriting the compact summary's three-level display
cut; the funnel still selects only its nearest qualifying level.

`data_state` handling is deliberately three-way:

- `fresh` is the required proof before a regular-evidence or RSI-only-fail
  candidate can be counted.
- An explicit non-fresh value (`stale`, `partial`, `degraded`, and so on) fails
  `base_eligibility` and stops downstream stages.
- A missing or unrecognised key is `freshness.status="undetermined"`, not a
  fresh pass and not a drop reason. If price and restriction evidence are
  otherwise present, later stages are recorded as observation-only, while both
  eligibility counters remain zero. This distinguishes an unavailable
  freshness proof from a real stale result without removing the freshness gate.

The recorded funnel is always in this order:

`source → base_eligibility → support_source_count → upside → rsi → anchor_band → budget`

The RSI stage is either `regular_pass` or `rsi_only_fail`. The latter remains a
classification only; this tool never turns it into an actionable result. The
anchor band is deliberately pre-tick and non-executable. A future separately
authorized consumer would need to redo tick-floor and all execution gates.

The literal policy gates remain unchanged and are runtime-pinned: RSI 45;
moderate, two-family support within 8%; honest upside of at least 40%; support
discount 5–10%; final anchor distance -15% to -5%; and the 90% / 50% budget
caps. The upside stage is fail-closed on ROB-486 window metadata
(`rows_excluded_stale` / `stale_opinion_count` > 0): leftover
`avg_target_price` from remaining in-window rows is not a pass
(`honest_upside_stale_inputs`). Because this tool cannot inspect account or
broker evidence, a reached `budget` stage is always `deferred` and
`actionable_count` is always zero.

## KR regular-session missing-freshness contract

The following is a synthetic unit-test fixture for the historic KR
regular-session compact-shaped reply (not a live MCP call). Its response has
price/support/consensus/RSI but no `data_state`. It proves that a missing key
does not make the funnel look like there were no candidates, while it still
cannot create eligibility:

```json
{
  "symbol": "005930",
  "matched_sources": ["rsi"],
  "freshness": {
    "status": "undetermined",
    "reason": "freshness_data_state_missing",
    "eligibility_blocked": true
  },
  "funnel": {
    "source": {"status": "pass"},
    "base_eligibility": {"status": "undetermined", "reason": "freshness_data_state_missing", "continued_as_observation_only": true},
    "support_source_count": {"status": "pass", "eligibility_blocked_by_freshness": true},
    "upside": {"status": "pass", "eligibility_blocked_by_freshness": true},
    "rsi": {"status": "regular_pass", "eligibility_blocked_by_freshness": true},
    "anchor_band": {"status": "pass", "eligibility_blocked_by_freshness": true},
    "budget": {"status": "deferred", "eligibility_blocked_by_freshness": true}
  },
  "regular_evidence_eligible": false,
  "actionable": false
}
```

## 08-17 digest handoff

`digest_observation.source_stats` is the manual 08-17 digest payload. Per source
it records incoming rows, top-N clipping, deduped candidates, drop reasons,
regular-evidence candidates, RSI-only-fail candidates, and final actionable
count under `final_eligible_counts`. `funnel_stage_counts` records every stage
status (including `base_eligibility: undetermined`), while
`freshness_undetermined_reasons` is kept separate from `dropped_reasons`.

For live sources, `source_population` is either `reported` when the upstream
returns a total or `bounded_unknown` with the explicit
`upstream_total_not_reported_with_top_n_only_read` reason. The fan-out never
turns that metadata into an unbounded read.

It is an observation only: do not use it as PnL scoring or as immediate
threshold-tuning evidence. Any later measurement or threshold-policy change
must be independently designed and operator reviewed.

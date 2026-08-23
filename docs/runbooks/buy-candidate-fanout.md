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

## Threshold proximity (near-miss) tagging — ROB-1315 §7-3

Recording only. **No verdict moves.** A candidate rejected by a numeric gate is
still rejected; the tag exists so the negative-class cohort can distinguish a
reject at 45.03 against a 45 ceiling from one at 78.

Motivating cases (2026-08-21 US session): CIEN RSI 45.03 vs 45 (subsequent MFE
+19.39%) and RDDT honest upside 39.93% vs 40% (MFE +20.09%). Both were graded
"correctly rejected" alongside rejects that missed by tens of points, so the
margin claim could not be tested.

### Band

`app/services/threshold_proximity.PROXIMITY_BAND = 1.0`, applied as **±1.0 in
the gate's own unit** — RSI points for `screen.rsi_max`, percentage points for
the two percent gates. That is the reading which admits both cited cases. The
relative distance is recorded too (`miss_pct_of_threshold`), so a scorer can
re-filter on the stricter "1% of the threshold" reading without re-deriving
anything. The band is a module constant, not a parameter: widening it
mid-collection would change which rejects are in the cohort.

### Tagged gates

| Gate | Metric | Threshold | Comparison |
| --- | --- | --- | --- |
| `screen.rsi_max` | `rsi_14` | 45 | `max` |
| `buy.support_reserve_net.honest_upside_pct_min` | `honest_upside_pct` | 40 | `min` |
| `buy.support_reserve_net.support_within_current_pct_max` | `support_distance_pct` | 8 | `max` |

Non-numeric failures (missing consensus, stale window inputs, freshness,
trading restriction, family count, strength tier) are **not** tagged: a gate
that failed for lack of data is an absent measurement, not a marginal
rejection. A missing observation is never invented into a tag.

### Where it appears

- per stage: `funnel.<stage>.threshold_proximity` (only on the failing stage)
- per candidate: `candidates[].threshold_proximity` (list) and
  `candidates[].negative_class_forecast_hint`
- aggregate: `digest_observation.threshold_proximity` — `band`, `by_gate`,
  `candidate_count`, `candidates`, `recording_only: true`,
  `gate_verdict_changed: false`

Because the funnel short-circuits, a candidate carries at most one tag per run:
the first numeric gate it failed. That is intentional — the later gates were
never evaluated, so there is no observation to tag.

### How to record it

This tool writes nothing. Merge the hint into the negative-class record the
session already makes (ROB-1283):

```python
hint = candidate["negative_class_forecast_hint"]
forecast_save(
    created_by="kr-open-trade",
    symbol=candidate["symbol"],
    instrument_type="equity_kr",
    forecast_target={**your_resolvable_target, **hint},
    probability=0.30,
    review_date="2026-09-19",
    decision_bucket="deferred_no_action",   # hint["decision_bucket_hint"]
)
```

Score this cohort separately after four weeks. Do not use an intermediate read
of it to move a threshold — that is the same pre-registration rule ROB-1301
runs under.

## Consensus-window scope and limits

This is a window-membership gate, not a maximum-age guarantee. It fails only
when existing ROB-486 metadata says a **dated** opinion fell outside the
configured opinion window. An all-in-window set can still be near that window's
cutoff, and ROB-488 keeps undated rows fail-open while exposing
`rows_undated`; neither condition is reclassified as fresh by this runbook.
`limit` and `opinion_window_months` change the observed opinion set for the
general `get_investment_opinions` surface, so consumers must not retry with a
smaller limit or wider window to manufacture a numeric upside. This fanout's
own fresh revalidation uses its fixed provider call and reports the metadata it
received. The gate does not cover TVScreener-provided KR `avg_target`/
`upside_pct` fields, which do not carry ROB-486 opinion-window metadata.
`target_price_honest` reports target-price exclusions only, not whole-opinion
freshness: it can be `true` while every target statistic and `upside_pct` is
null when a dated window-external opinion has no positive target price; consult
`rows_excluded_stale`/`stale_opinion_count` before treating that combination as
usable consensus.

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

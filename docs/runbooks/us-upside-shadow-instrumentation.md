# US upside shadow instrumentation

This is a manual, read-only logging contract for three consecutive completed
US regular sessions. It does not collect sources itself and it does not call a
broker or account, create a proposal, alter eligibility, write a database,
register a scheduler, deploy anything, or modify a production policy.

The controlling contract is §Q4 of
`/Users/mgh3326/work/herdr-inbox/answer-codexmock-upside-0814.md`.
Its frozen SHA-256 is
`2b8044a1cc39fbd830f68a3253ebdb20db987f2d9f76763edec8a2e3af3a5fe6`.

## Frozen arms

Before the first observation, the implementation freezes exactly these shadow
arms:

```text
A40 = current 40% path
B30 = 30% + strong + 3-family + final distance 12–15%
C25 = 25% + strong + 3-family + final distance 11–15% (diagnostic only)
```

`app/services/us_upside_instrumentation.py::FROZEN_ARMS` is the only arm
definition. The CLI has no arm, threshold, distance, or policy override. Each
result is a `would_select` counterfactual and contributes only to an
`arm_shadow_counts` number; `read_only_safety` is fixed at zero for every
runtime-facing count.

## Input and persisted record

The upstream capture is JSON validated by `InstrumentationInput`. It must
include the following required decision-time fields. A nullable field is still
present in the JSON when its value is not known.

| Required §Q4 evidence | Input / log field | Implementation location |
| --- | --- | --- |
| Contract, policy, and code SHA | `contract_sha`, `policy_sha`, `code_sha` | `InstrumentationInput`, `SessionRecord` |
| Corpus as-of, decision cutoff, universe/input hashes | `source_corpus_as_of`, `decision_cutoff`, `universe_hash`, derived `input_hash` | `InstrumentationInput`, CLI `_load_snapshot` |
| Per-source upstream known/unknown total, returned, timeout/error, outside top-N, deduped unique | `sources[*].upstream_total_known`, `upstream_total_unknown`, `returned_count`, `timeout_or_error_count`, `outside_top_n_count`, `deduped_unique_count` | `SourceCoverage` |
| Explicit unqueried count | `sources[*].unqueried_count` | `SourceCoverage` / `CoverageSummary` |
| Matched source IDs and ranks | `candidates[*].matched_sources[*].source_id`, `rank` | `MatchedSource` |
| Fresh/stale/unknown and target evidence | `freshness`, `consensus_status`, `target_honesty`, `target_as_of`, `analyst_count` | `CandidateSnapshot` |
| Current price, target, upside, RSI | `current_price`, `target`, derived `upside_pct`, `rsi` | `CandidateSnapshot`, `CandidateInstrumentationRecord` |
| Support price, strength, independent families, distance | `support_price`, `support_strength`, `independent_support_families`, derived `support_distance_pct` | `CandidateSnapshot`, `CandidateInstrumentationRecord` |
| Limit, tick treatment, final discount, arithmetic limit-basis upside | `proposed_limit`, `tick_handling`, derived `final_discount_from_current_pct`, `arithmetic_limit_basis_upside_pct` | `TickHandling`, `CandidateInstrumentationRecord` |
| Exact gate bits and reject reasons per arm | `arm_results[*].gate_bits`, `reject_reasons` | `_evaluate_arm` / `ArmGateResult` |
| Sector/dedupe/cash/whole-share feasibility, would-size/cash, would-select | `feasibility`, `arm_results[*].would_select` | `Feasibility` / `_evaluate_arm` |
| Optional following-session high/low touch observation | `hypothetical_limit_touch` | `HypotheticalLimitTouch` |

`SourceCoverage` is intentionally strict: a source must say whether its total
is known, and it must always provide numeric timeout/error, unqueried, and
outside-top-N counts. A top-N cap is represented by `top_n_cap`; when absent,
`outside_top_n_count` must be zero. This makes bounded scope visible instead
of silently dropping it.

`hypothetical_limit_touch` is an always-present nullable field; its object is
optional and is an observation only. When present it has `next_session_high`,
`next_session_low`, and `limit_touched`; it is never a trading or performance
result.

### Capture JSON shape

The input is one JSON object, not JSONL. This abbreviated valid shape shows
where an explicit unknown is represented as `null` plus its state flag rather
than omitted:

```json
{
  "session_id": "2026-08-14-rth",
  "contract_sha": "2b8044a1cc39fbd830f68a3253ebdb20db987f2d9f76763edec8a2e3af3a5fe6",
  "policy_sha": "<sha256>",
  "code_sha": "<git-commit-sha>",
  "source_corpus_as_of": "2026-08-14T22:35:00+09:00",
  "decision_cutoff": "2026-08-14T22:35:00+09:00",
  "universe_hash": "<sha256>",
  "sources": [
    {
      "source_id": "upstream-source",
      "upstream_total_known": 10,
      "upstream_total_unknown": false,
      "returned_count": 3,
      "timeout_or_error_count": 0,
      "unqueried_count": 0,
      "top_n_cap": 3,
      "outside_top_n_count": 7,
      "deduped_unique_count": 2
    }
  ],
  "candidates": [
    {
      "symbol": "EXAMPLE",
      "matched_sources": [{"source_id": "upstream-source", "rank": 1}],
      "freshness": "fresh",
      "consensus_status": "value",
      "target_honesty": "honest",
      "target_as_of": "2026-08-14T22:30:00+09:00",
      "analyst_count": 4,
      "current_price": 100.0,
      "target": 130.0,
      "rsi": 42.0,
      "support_price": 90.0,
      "support_strength": "strong",
      "independent_support_families": ["price", "volume", "trend"],
      "non_upside_gate_bits": {"source": "pass", "freshness": "pass"},
      "proposed_limit": 87.0,
      "tick_handling": {
        "rule": "upstream tick rule",
        "raw_limit": 87.004,
        "snapped_limit": 87.0,
        "direction": "down"
      },
      "feasibility": {
        "sector": "Example",
        "sector_feasibility": "pass",
        "dedupe_feasibility": "pass",
        "cash_feasibility": "pass",
        "whole_share_feasibility": "pass",
        "would_size": 1.0,
        "required_cash": 87.0
      },
      "hypothetical_limit_touch": null
    }
  ]
}
```

`policy_sha`, `code_sha`, and `universe_hash` above are placeholders only in
the documentation. A real capture must use their exact decision-time values.

## Fixed reading rules

`_interpret_session` and `read_three_completed_sessions` encode these rules;
the tests cover their decision branches.

1. If `passes_all_non_upside_gates > 0`, every survivor consensus state is
   `value`, `missing`, `stale`, or `error`, no timeout remains, `>=40` is zero,
   and `25~40` is positive, the only conclusion is that upside is the dominant
   constraint in that bounded cohort.
2. If there are zero survivors before the upside check, the constraint is an
   earlier source, freshness, support, or other declared non-upside gate.
3. Any timeout, unqueried state, or unknown coverage produces
   `coverage_insufficient_no_threshold_conclusion`; it does not answer a
   threshold question.
4. A larger unique-fresh result from source fan-out is not evidence of
   performance or alpha. The record therefore fixes
   `fanout_performance_or_alpha_inferred=false`.
5. Once exactly three sessions are read, zero B30 and C25 counts lead only to
   `diagnose_target_coverage_or_support_source_contract`; the reader has no
   below-25 action or threshold-change output.

## Manual three-session procedure

The authorized executor is the US session's upstream read-only
consumer/operator. This job supplies the validator and logger only; it does
not perform the collection. Start with the 22:35 KST US session tonight and
repeat once per next two completed US regular sessions.

1. At the declared decision cutoff, the upstream executor creates one bounded
   source snapshot under an operator-owned directory, for example
   `/Users/mgh3326/work/us-upside-captures/20260814-2235.json`. The upstream
   process must be read-only. It must populate every source census field,
   every candidate field in the table above, and the three SHA fields.
2. Before recording, verify the contract and capture provenance without
   changing anything:

   ```bash
   shasum -a 256 /Users/mgh3326/work/herdr-inbox/answer-codexmock-upside-0814.md
   git rev-parse HEAD
   shasum -a 256 config/trading_policy.yaml
   ```

   Put the exact values into `contract_sha`, `code_sha`, and `policy_sha` in
   the snapshot. `contract_sha` must equal the frozen full SHA above.
3. Record the snapshot after its regular-session capture is complete. No
   environment variable is required or read by this command; do not arm an
   execution environment.

   ```bash
   uv run python -m scripts.run_us_upside_instrumentation record \
     --input /Users/mgh3326/work/us-upside-captures/20260814-2235.json \
     --output /Users/mgh3326/work/us-upside-captures/us-upside-shadow.jsonl
   ```

   The only write is the explicitly named local JSONL output. Save its stdout
   next to the session evidence.
4. Stop the collection immediately if a required JSON field, SHA, source
   count, cap count, or validation result is absent or malformed. If a source
   genuinely has an unknown total, timeout/error count, or unqueried count,
   record that state explicitly instead of substituting a value; the log will
   then be coverage-insufficient and must not be used to infer a threshold.
5. Do not add following-session high/low data until it exists. If it is later
   available, it may be captured as `hypothetical_limit_touch` in a subsequent
   immutable observation record. Do not turn that observation into an
   execution or performance claim.
6. After exactly three completed session records exist, run the fixed reader:

   ```bash
   uv run python -m scripts.run_us_upside_instrumentation read-three \
     --records /Users/mgh3326/work/us-upside-captures/us-upside-shadow.jsonl
   ```

   Read each session with Rules 1–4, then use the three-session output only
   for Rule 5. The reader requires one contract, policy, and code SHA across
   all three records. Never use candidate counts or touch observations to
   choose a threshold.

## Stop conditions and scope boundary

- A validation error, contract SHA mismatch, missing cap census, invalid
  JSONL, source access failure, or any attempted connection to a runtime
  mutation surface is a stop condition.
- An explicit unknown/timeout/unqueried state is not silently repaired; it is
  recorded and makes the threshold conclusion unavailable.
- The command accepts no credentials and has no broker/account, proposal,
  eligibility, database, scheduler, deployment, or production-policy path.
- The US collection must not be implemented by extending the KR-only fan-out
  surface.

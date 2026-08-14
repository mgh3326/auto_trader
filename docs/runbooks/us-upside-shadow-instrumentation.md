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
| Per-source upstream known/unknown total, returned, timeout/error, top-N cap/outside, deduped unique, declared duplicates | `sources[*].upstream_total_known`, `upstream_total_unknown`, `returned_count`, `timeout_or_error_count`, `unqueried_count`, `top_n_cap`, `outside_top_n_count`, `deduped_unique_count`, `duplicate_count` | `SourceCoverage` |
| Explicit unqueried count | `sources[*].unqueried_count` | `SourceCoverage` / `CoverageSummary` |
| Global post-dedupe candidate census, declared cross-source duplicates, and candidate-array truncation | `candidate_array_coverage.deduped_unique_count`, `cross_source_duplicate_count`, `recorded_candidate_count`, `truncated_candidate_count`, `candidate_array_cap`, `candidate_truncation_reason` | `CandidateArrayCoverage`, `CoverageSummary` |
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
outside-top-N counts. When the upstream total is known, it must equal
`returned_count + timeout_or_error_count + unqueried_count +
outside_top_n_count`; those loss categories are therefore disjoint and no
bounded-read remainder can disappear. A top-N cap is represented by
`top_n_cap`; when absent, `outside_top_n_count` must be zero.

The next source boundary is also explicit:
`returned_count == deduped_unique_count + duplicate_count`. A missing or
mismatched `duplicate_count` is rejected as coverage-insufficient before a
session record or threshold conclusion can be emitted. This count represents
only source rows verified as duplicates; it is not a place to conceal a bound.

`CandidateArrayCoverage` performs the same explicit accounting after global
dedupe: `deduped_unique_count` must equal `recorded_candidate_count +
truncated_candidate_count`. A positive truncation count requires both the
candidate-array cap and a non-empty reason. Any nonzero top-N-outside or
candidate-array-truncated count makes `coverage_complete=false`.

The global union boundary is explicit as well. Across all sources,
`sum(deduped_unique_count) == global deduped_unique_count +
cross_source_duplicate_count`, and the global count must be at least the
largest one-source unique count. A missing or inconsistent cross-source count
is rejected as coverage-insufficient before a session record or threshold
conclusion can be emitted.

Every matched source object must include `rank`; use `null` when an upstream
source cannot provide a rank rather than omitting the field.

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
      "deduped_unique_count": 2,
      "duplicate_count": 1
    }
  ],
  "candidate_array_coverage": {
    "deduped_unique_count": 2,
    "cross_source_duplicate_count": 0,
    "recorded_candidate_count": 1,
    "truncated_candidate_count": 1,
    "candidate_array_cap": 1,
    "candidate_truncation_reason": "bounded_candidate_array"
  },
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

### Capped top-N example: required coverage result

`tests/fixtures/us_upside_instrumentation/runbook_top_n_capped_capture.json`
is an executable example with `upstream_total_known=10`, `top_n_cap=3`,
`returned_count=3`, and `outside_top_n_count=7`. Run it from the repository
root with a disposable local path:

```bash
uv run python -m scripts.run_us_upside_instrumentation record \
  --input tests/fixtures/us_upside_instrumentation/runbook_top_n_capped_capture.json \
  --output /tmp/us-upside-top-n-capped.jsonl
```

Its stdout must contain `"coverage_complete": false` and
`coverage_insufficient_no_threshold_conclusion`. The seven records outside
the top-N bound must never yield a threshold conclusion.

### Coverage-complete example

`tests/fixtures/us_upside_instrumentation/runbook_coverage_complete_capture.json`
is the separate complete-census example: one returned row, one source unique,
zero source duplicates, zero cross-source duplicates, and no candidate-array
truncation. Run it with a separate disposable path:

```bash
uv run python -m scripts.run_us_upside_instrumentation record \
  --input tests/fixtures/us_upside_instrumentation/runbook_coverage_complete_capture.json \
  --output /tmp/us-upside-coverage-complete.jsonl
```

Its stdout contains `"coverage_complete": true`; its bounded reading is not a
performance claim and cannot change a threshold.

## Fixed reading rules

`_interpret_session` and `read_three_completed_sessions` encode these rules;
the tests cover their decision branches.

The frozen §Q4 wording is retained here without a threshold-change action:

```text
1. `passes_all_non_upside_gates > 0` 이고, 그 집합의 consensus 상태가 모두 value/missing/stale/error
   로 종결됐고 timeout 이 없는데 `>=40 == 0` 이며 `25~40 > 0` 이면
   → 해당 bounded cohort 에서 upside 가 지배 제약이다
2. upside 전에 survivors 가 0 이면 지배 제약은 source·freshness·support 등 앞단이다
3. 40/99 처럼 timeout·미조회·unknown 이 남으면 coverage 불충분이며 threshold 에 관해 모름이다
4. FAN-OUT 으로 unique fresh 후보가 늘어도 성과나 alpha 가 개선됐다는 뜻은 아니다
5. 3세션 뒤 B/C 가 0 이어도 25 아래로 더 내리지 않는다 — target coverage 나 support-source
   계약을 먼저 진단한다
3세션 후보 수나 hypothetical touch 로 threshold 를 튜닝하지 마라
```

1. If `passes_all_non_upside_gates > 0`, every survivor consensus state is
   `value`, `missing`, `stale`, or `error`, no timeout remains, `>=40` is zero,
   and `25~40` is positive, the only conclusion is that upside is the dominant
   constraint in that bounded cohort.
2. If there are zero survivors before the upside check, the constraint is an
   earlier source, freshness, support, or other declared non-upside gate.
3. Any timeout, unqueried state, unknown coverage, top-N-outside count,
   source/global dedupe-census mismatch, or candidate-array truncation produces
   `coverage_insufficient_no_threshold_conclusion`; it does not answer a
   threshold question. In the three-session reader, one incomplete session
   produces the same result for the aggregate: no upside-dominant or
   non-dominant conclusion is available. A dedupe-census mismatch is rejected
   before it can produce a session verdict, which is the same fail-closed
   outcome.
4. A larger unique-fresh result from source fan-out is not evidence of
   performance or alpha. The record therefore fixes
   `fanout_performance_or_alpha_inferred=false`.
5. Once exactly three sessions are read, zero B30 and C25 counts lead only to
   `diagnose_target_coverage_or_support_source_contract`; the reader has no
   below-25 action or threshold-change output.

## Truncation and coverage census

Every bounded-read or aggregation loss point is declared below. A capture or
reading with any unaccounted point is rejected or remains
coverage-insufficient; none is treated as a complete cohort.

| Boundary | Required numeric / state record | Arithmetic enforcement | Verdict effect |
| --- | --- | --- | --- |
| Upstream collection outside this repository | `upstream_total_known` or `upstream_total_unknown` for every source | `SourceCoverage` requires exactly one state | Unknown total makes coverage incomplete |
| Per-source top-N bound | `top_n_cap`, `outside_top_n_count` | Known total equals returned + timeout/error + unqueried + outside top-N | Any outside count makes coverage incomplete |
| L1: returned to source unique | `returned_count`, `deduped_unique_count`, `duplicate_count` | `returned == unique + duplicate` | Missing/mismatch is rejected as coverage-insufficient before a verdict |
| L2: source unique to global unique | per-source `deduped_unique_count`, global `deduped_unique_count`, `cross_source_duplicate_count` | `sum(source unique) == global unique + cross-source duplicate` and `global >= max(source unique)` | Missing/mismatch is rejected as coverage-insufficient before a verdict |
| L3: global unique to recorded candidates | `deduped_unique_count`, `recorded_candidate_count`, `truncated_candidate_count`, `candidate_array_cap`, `candidate_truncation_reason` | `global unique == recorded + truncated`; positive truncation requires cap and reason | Any truncation makes coverage incomplete |
| L4: recorded candidates to serialized array | `recorded_candidate_count`, `candidates` | declared recorded count equals array length | Mismatch is rejected before a verdict |
| Timeout or error | `timeout_or_error_count` | Required per source and aggregated in `CoverageSummary` | Any count makes coverage incomplete |
| Not queried | `unqueried_count` | Required per source and aggregated in `CoverageSummary` | Any count makes coverage incomplete |
| Three-session aggregate | `session_coverage[*]`, `all_sessions_coverage_complete` | reader recomputes each stored coverage flag from its counts | One incomplete session terminates as coverage-insufficient; a stored-flag mismatch is rejected |

## Manual three-session procedure

The authorized executor is the US session's upstream read-only
consumer/operator. This job supplies the validator and logger only; it does
not perform the collection. Start with the 22:35 KST US session tonight and
repeat once per next two completed US regular sessions.

1. At the declared decision cutoff, the upstream executor creates one bounded
   source snapshot under an operator-owned directory, for example
   `/Users/mgh3326/work/us-upside-captures/20260814-2235.json`. The upstream
   process must be read-only. It must populate every source census field,
   including `duplicate_count`; the global post-dedupe candidate census,
   including `cross_source_duplicate_count` and any candidate-array
   cap/loss/reason; every recorded candidate field in the table above; and the
   three SHA fields.
   `duplicate_count` 는 동일 심볼 반복만 센다. 숫자를 맞추기 위해 잔여를 중복으로 쓰지 마라. 설명 불가면 record 를 쓰지 마라.
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
   count, top-N count, duplicate count, cross-source duplicate count,
   candidate-array count, cap, truncation reason, or validation result is
   absent or malformed. Verify both dedupe equations in the census table. If a
   source genuinely has an unknown total, timeout/error count, unqueried count,
   top-N-outside count, dedupe-census mismatch, or candidate-array truncation,
   record or stop on that state explicitly instead of substituting a value; it
   is coverage-insufficient and must not be used to infer a threshold.
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
   all three records. If even one session reports incomplete coverage, the
   aggregate terminates as `coverage_insufficient_no_threshold_conclusion`.
   The reader also rejects a stored `coverage_complete` value that does not
   recompute from its saved counts. Never hand-edit that boolean.
   Never use candidate counts or touch observations to choose a threshold.

   Keep exactly one line per distinct `session_id` in this artifact. The
   recorder rejects a duplicate `session_id`; if an input correction is
   needed, preserve the first artifact and use a new explicitly named output
   for the corrected observation. `read-three` deliberately rejects any file
   with a count other than three rather than selecting lines silently.

## Stop conditions and scope boundary

- A validation error, contract SHA mismatch, missing top-N, source-duplicate,
  cross-source-duplicate, or candidate-array census, invalid JSONL, source
  access failure, or any attempted connection to a runtime mutation surface is
  a stop condition.
- An explicit unknown/timeout/unqueried/top-N-outside/dedupe-census-mismatch/
  candidate-array-loss state is not silently repaired; it is recorded or
  rejected as coverage-insufficient and makes the threshold conclusion
  unavailable.
- The command accepts no credentials and has no broker/account, proposal,
  eligibility, database, scheduler, deployment, or production-policy path.
- The US collection must not be implemented by extending the KR-only fan-out
  surface.

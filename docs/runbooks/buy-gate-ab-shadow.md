# Buy-gate A/B shadow — pre-registered experiment (ROB-1301)

Observation-only. Variant A is the live KR/US buy screen (strong support
required). Variant B is the moderate+ support counterfactual. This runbook
is the scoring-report spec; it does not authorize an order, a proposal, a
watch, a policy edit, or a scheduler.

Controlling issue: [ROB-1301](https://linear.app/mgh3326/issue/ROB-1301).
Code pin: `app/services/buy_gate_ab_shadow/spec.py` (`PINNED_SPEC_SHA256`).

🔴 **A second, stricter pre-registration is registered but not collecting.**
`docs/preregistrations/2026-08-22-support-strength-two-source-equivalence.md`
(retro §7-1, confirmed §139차 ②) declares a variant B of *moderate AND
source_count>=2 AND independent families*, adds crypto `winner_pullback_add`,
and adds a pre-committed numeric promotion rule. **That is not the experiment
this runbook and the code pin describe.** Do not mix the two cohorts. Its
start conditions (§7 of that file) are unmet, so everything below still
describes the live, pinned ROB-1301 experiment.

## Forbidden (issue canonical, not paraphrased)

* shadow가 제안·주문·워치로 승격 금지(순수 기록)
* 라이브 게이트 문언 무접촉
* 채점 전 중간값으로 정책 변경 논거 삼지 않기(사전 등록 원칙)

Promotion, automation, and TaskIQ/cron/Prefect triggers are **0**. Mock
accounts are not used (1 account = 1 strategy).

## Hypothesis (pre-registered)

"strong 지지 요구가 기대값 양(+)인 후보를 과도하게 기각한다" — KR 매수
기각 4/6 (2026-08-20) · US 9세션 사인 다수가 지지 품질.

Do not amend the hypothesis after seeing scores.

## Design

| Item | Frozen value |
| --- | --- |
| Markets | KR (priority), then US. Crypto out of scope. |
| Variant A | live; `support_strength_min=strong`; executes |
| Variant B | shadow; `support_strength_min=moderate`; does not execute |
| Shared gates | RSI < 45, support within 8%, honest upside ≥ 40%, plus session bits `liquid_midcap` / `concentration` / `overhang` |
| Only difference | `support_strength_min` |
| Entry | decision-time current price, frozen |
| Assumed sizing | live cap × 0.5 (KRW 400,000 → 200,000; USD 450 → 225) |
| Windows | 5 trading days and 20 trading days |
| Collection | 28 calendar days |
| Combine with | ROB-1283 tight-reject recovery data |

A and B must receive the **same candidate snapshot**, the **same
`evaluation_as_of`**, and (at score time) the **same `scoring_as_of`**.
Giving one arm later bars, a different entry, or a different other-gate
bit is a contract violation; the evaluator/scorer drop bars after
`scoring_as_of` and do not impute holes.

## Candidate input contract (ROB-1315 §5-1)

**An unknown key is rejected, not ignored.** On 2026-08-21 a US session sent
`rsi_14` and `nearest_support_strength`; both were silently dropped, both
fields read as absent, and all seven candidates were rejected for gates they
would have passed. Nothing in the response said so. The evaluator now refuses
the call and names the correct key.

| Key | Required | Type | Notes |
| --- | --- | --- | --- |
| `symbol` | yes | string | upper-cased |
| `market` | yes | `kr` \| `us` | crypto is out of scope |
| `current_price` | yes | number/decimal string | must be > 0; this is the frozen entry |
| `support_strength` | no | `weak` \| `moderate` \| `strong` | the A/B fork; an unknown word is rejected |
| `support_distance_pct` | no | number | percent below current price |
| `rsi` | no | number | **`rsi`, not `rsi_14`** |
| `honest_upside_pct` | no | number | percent |
| `other_gate_bits` | no | object of booleans | keys exactly `liquid_midcap`, `concentration`, `overhang` |

Rejection names the offending row (`candidates[1] (RDDT): ...`) and echoes
`input_contract`, whose `common_mistakes` map spells out the correction. The
same `input_contract` block rides on successful responses, so the contract is
readable without triggering an error.

An **omitted optional field is still a rejection** — a gate cannot pass on
absent evidence — but that is now the caller's explicit choice rather than a
typo the evaluator swallowed. Do not fill a field with a guess to make a
candidate pass.

## 🔴 US overhang: structurally uncollectable today

`other_gate_bits.overhang` has **no US data source in this repo**.
`get_disclosures` is DART/KR-only; there is no SEC EDGAR client, and the
`market_events` `lockup_expiry` category is declared but has no ingester.

Measured on 2026-08-21: the valid US call returned `n=7 · a_and_b 0 · b_only 0
· neither 7`, with CRH and UPST failing on `overhang` alone under variant B.
The session set `overhang=false` for all seven rather than inventing a pass —
that was correct, and it is also why US B-only is pinned at zero.

**Do not work around this by setting `overhang=true`.** Options (neutralize and
flag / build an EDGAR ingester / drop US from scope) are laid out in
`docs/superpowers/specs/2026-08-22-us-overhang-gate-design.md` and are
**awaiting an operator decision**. Until one is approved, US shadow logic is
unchanged and the US arm is not collecting. KR collection is unaffected.

## Session procedure

1. Run the live screen as today (variant A). Winners still go through
   `order_proposal_create`. Nothing here relaxes that path.
2. Call `evaluate_buy_gate_ab_shadow(candidates, evaluation_as_of, created_by)`
   on the **same** reviewed set. One `evaluation_as_of` per call.
3. For `cohort=b_only` rows only, persist the returned
   `shadow_buy_forecasts` via `forecast_save`. Do not invent tags.
4. Do not create a proposal, order, or watch from a B-only row. Do not
   use a mock account as a consolation prize.

`evaluate_buy_gate_ab_shadow` does not write. Forgetting `forecast_save`
is a missed record, not a live-path change.

## Forecast tagging

Every shadow row must carry:

| Field | Value |
| --- | --- |
| `session_label` | `rob-1301-buy-gate-ab-shadow` |
| `correlation_id` | `{experiment_id}:{market}:{symbol}:{YYYY-MM-DD}:{window}d` |
| `forecast_target.kind` | `price_target` |
| `forecast_target.cohort` | `shadow_buy` |
| `forecast_target.variant` | `B` |
| `forecast_target.promote` | `false` |
| `forecast_target.spec_sha256` | `PINNED_SPEC_SHA256` at the moment of registration |
| `forecast_target.evaluation_as_of` | timezone-aware decision timestamp |
| `forecast_target.input_snapshot` / `_sha256` | the normalized shared A/B gate input and its digest |
| `forecast_target.calibration_eligibility` | `calibration_exclude` |
| `forecast_target.trade_performance_eligibility` | `trade_performance_exclude` |
| `forecast_target.scoring_authority` | `rob-1301-buy-gate-ab-shadow.scoring` |
| `horizon` | `5d` or `20d` (two rows per candidate) |

`forecast_resolve` Brier is **not** the experiment score. The 4-week
report uses `app.services.buy_gate_ab_shadow.scoring`.

`forecast_save` is the persistence boundary: when it receives this exact
ROB-1301 B-only target, it appends a `review.sample_eligibility_decisions`
forecast decision with `calibration_eligibility=calibration_exclude`. The
calibration aggregate reads that decision table, not this JSON tag alone. A
malformed purported shadow target is rejected before it is saved.

## 4-week scoring report spec

Run once `scoring_as_of.date() >= collection_start + 27 days` and every
sample that should have a 20-session window can be supplied that many
regular-session bars with `session_date <= scoring_as_of`. Until then
`compare_cohorts` returns its collection status only and refuses to compute
or return intermediate returns/drawdowns; `policy_implication` stays
`none_until_collection_complete`.

### Cohorts

* **A live-executed** — variant A passed and a live order was sent.
  Primary entry is still the frozen decision price, not the fill.
* **A tight-reject** — support quality was the A failure (B-only).
  Combine with ROB-1283 structured rejects when those recover.
* **B shadow** — the same B-only set, scored from the frozen entry.

### Primary metrics (both arms, both windows)

1. `simple_return_to_close` = `(close_N - entry) / entry`
2. `max_drawdown_from_entry_close_peak` — running peak starts at `entry`,
   then each window close; drawdown = `(close - peak) / peak`; report the
   minimum (most negative).

### Sensitivity (always computed, never promoted to primary)

* `simple_return_to_window_high`
* `simple_return_to_window_low`
* A-executed actual-fill return vs frozen entry (observational only)

Missing bars are `unscoreable` (`insufficient_bars`). Do not forward-fill.
Do not extend collection because N looks small. Do not drop names after
seeing outcomes. Do not declare a winner in the report.

### Report shape

`compare_cohorts(...)` already emits the required envelope:
`collection_complete`, `status`, `policy_implication`,
`intermediate_use_forbidden`, `winner_declaration=forbidden`, per-arm
window primary + sensitivity, and `combine_with=ROB-1283`.

Copy that JSON. Do not retune thresholds from it in the same change.

## What this does not do

* It does not edit `config/trading_policy.yaml`.
* It does not change `buy.support_reserve_net` (`moderate` there is a
  different tier and stays as-is).
* It does not call `order_proposal_create`, `place_order`, or watch
  create.
* It does not register a schedule.

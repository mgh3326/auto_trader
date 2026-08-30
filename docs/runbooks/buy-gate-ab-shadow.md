# Buy-gate A/B shadow — pre-registered experiment (ROB-1301)

Observation-only. Variant A is the live KR/US buy screen (strong support
required). Variant B is the moderate+ support counterfactual. This runbook
is the scoring-report spec; it does not authorize an order, a proposal, a
watch, a policy edit, or a scheduler.

Controlling issue: [ROB-1301](https://linear.app/mgh3326/issue/ROB-1301).
Code pin: `app/services/buy_gate_ab_shadow/spec.py` (`PINNED_SPEC_SHA256`).
Q6 activation addendum: ROB-1331
`rob-1331-q6-activation-epoch.v1`; immutable marker:
`review.buy_gate_ab_collection_epoch`.

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

## Q6 activation epoch addendum (ROB-1331, v1)

This is a versioned addendum to the original ROB-1301 pre-registration, not a
replacement chosen from observed records. The exact marker is:

| Field | Sealed value |
| --- | --- |
| `collection_armed_at` | `2026-08-30T09:17:36+09:00` |
| next complete eligible session / `collection_start` | `2026-08-31` (common KR/US regular-session date) |
| fixed window | 28 calendar days |
| last included calendar date | `2026-09-27` |
| `collection_end_exclusive` | `2026-09-28` |
| clock timezone | `Asia/Seoul` |
| policy projection SHA-256 | `c47ce8e132b7c88fa9e2554cdddc0f84663b467e115d45b79a07c618de9d857d` |
| amended pre-registration SHA-256 | `c07fb69001f5e48759718a4d725a327d5b6b1fb5d4aea442f3aeb7b170ffcd5b` |

The projection hash covers only the exact A/B decision surface: KR/US,
variant A `strong`, variant B `moderate`, the shared RSI/support-distance/
upside/three-bit gates, and `support_strength_min` as the only difference. It
does not live-read or hash unrelated policy keys.

The production migration creates and seeds exactly one row. PostgreSQL rejects
`UPDATE`, `DELETE`, and `TRUNCATE`; application code exposes the same marker as
a frozen dataclass. Operators apply
`alembic/versions/20260830_rob1331_q6_epoch.py` separately with
`uv run alembic upgrade head`. Do not activate any evaluator/`forecast_save`
caller wiring until that marker and this addendum are deployed and independently
reviewed. Wiring is a separate PR.

`first_valid_record_at` is not a marker column and is never a clock input. It
is a nullable observation derived from valid event rows. Whether it is null,
early, or late cannot move `collection_start` or `collection_end_exclusive`.

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
| `forecast_target.policy_projection_sha256` | exact Q6 policy projection pin |
| `forecast_target.collection_epoch_id` | `rob-1301-q6-collection-epoch.v1` |
| `forecast_target.collection_armed_at` | sealed timestamp above |
| `forecast_target.collection_start` / `collection_end_exclusive` | sealed 28-day boundary above |
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

`scoring_ready` is exactly `collection_window_closed AND
all_events_matured`. The collection window closes at the sealed exclusive
boundary (`2026-09-28` in the clock timezone); every observed event must then
have its full 20-session longest window. Satisfying only one side never permits
scoring. Until both are true, `compare_cohorts` returns status only and refuses
to compute or return intermediate returns/drawdowns; `policy_implication` stays
`none_until_collection_complete`.

Zero events do not hold the window open. With no event, universal maturity is
true, so the fixed window closes normally with
`status=INSUFFICIENT_SAMPLE`, `outcome=NO_FIRING`, and no score arms. Waiting
for a first row would make that row a post-hoc start selector and is forbidden.

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
* It does not add or activate the missing caller wiring; that is PR2 after this
  marker is merged, deployed, migrated, and independently reviewed.

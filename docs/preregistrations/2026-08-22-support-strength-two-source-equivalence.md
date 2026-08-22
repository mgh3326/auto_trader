# Pre-registration — `support_strength_two_source_equivalence`

- **Registered**: 2026-08-22
- **Status**: 🔴 **REGISTERED, NOT COLLECTING.** The declaration below is frozen.
  Collection has **not** started; the start conditions are in §7 and none of the
  blocking ones are met as of registration.
- **Origin**: buy-side multi-week retro 2026-08-22 §7-1 (ⓒ′)
  (`~/work/herdr-inbox/retro-buyside-multiweek-20260822.md`)
- **Confirmed as final by**: operator, relayed via claude-mock (상류 조정, wB:p4R),
  §139차 ② — *"회고 §7-1 사전등록 문언을 운영자 동의 확정본으로 docs/에 등록"*.
  🔴 This session did not receive operator sign-off directly; the confirmation
  reaching it is the relay instruction quoted above. That provenance is recorded
  rather than upgraded.
- **Related experiment**: ROB-1301 (`app/services/buy_gate_ab_shadow/`) —
  🔴 **this is not the same experiment as the one currently pinned in code.**
  See §5.
- **Execution impact**: 0. No order, proposal, watch, broker call, or scheduler.

---

## 1. The frozen declaration (verbatim, retro §7-1)

Reproduced character-for-character (verified byte-identical to the retro
source). `sha256` of the fenced body below — 12 lines, LF-terminated, fences
excluded: `7d6e9251ef04890cb94a411f4548e6323f61217a1597db2b012dcddadddde479`

```
사전등록 (shadow only, promote=false, calibration_exclude=true)
가설: regular discovery 및 winner_pullback_add의 support_strength_min을
      "strong" → "moderate AND source_count>=2 AND sources가 서로 독립 계열"로 치환하면
      게이트 통과율이 오르고, 통과 코호트의 D+5/D+20 수익률이 현행 코호트 대비 열등하지 않다.
적용 lane: KR/US regular discovery, crypto winner_pullback_add
수집 기간: 4주 (2026-08-22 ~ 2026-09-19), 목표 표본 n>=40 B-only
기록: 후보별 (변형A reject reasons, 변형B reject reasons, 지지 레벨/강도/sources,
      기각 시점가, D+1/D+5/D+20 종가, MFE, MAE)
승격 조건 (사전 확정): B-only 코호트 D+20 중앙값 >= 0 AND 하위 4분위 평균 > -6%
                       AND n >= 40. 하나라도 미달이면 기각한다.
집행 영향: 0. 이 실험은 주문·워치·제안을 만들지 않는다.
전제 조건: §5-1의 US overhang 구현 공백이 먼저 메워져야 US 표본이 수집된다.
```

## 2. Structured restatement (no content added)

| Field | Value |
| --- | --- |
| Experiment id | `support_strength_two_source_equivalence` |
| Mode | shadow only · `promote=false` · `calibration_exclude=true` |
| Variant A (live, unchanged) | `support_strength_min = strong` |
| Variant B (shadow) | `support_strength_min = moderate` **AND** `source_count >= 2` **AND** sources drawn from mutually independent families |
| Only difference | the support-quality clause above. Every other gate identical. |
| Lanes / markets | KR + US regular discovery; crypto `winner_pullback_add` |
| Collection window | 2026-08-22 → 2026-09-19 (28 calendar days) |
| Sample target | `n >= 40` B-only |
| Recorded per candidate | variant A reject reasons · variant B reject reasons · support level/strength/sources · price at rejection · D+1, D+5, D+20 closes · MFE · MAE |
| Promotion (pre-committed) | B-only cohort D+20 **median ≥ 0** AND **bottom-quartile mean > −6%** AND **n ≥ 40**. Any one short ⇒ **reject**. |
| Execution impact | 0 |
| Precondition | the §5-1 US overhang implementation gap must be closed before US samples can be collected |

🔴 **The promotion rule is now pre-committed and may not be re-derived after
seeing data.** "Bottom quartile" is the lowest 25% of the B-only cohort's D+20
returns; its arithmetic mean must exceed −6%. Ties at the quartile boundary
resolve by including the boundary observation in the bottom quartile (the
conservative side). This tie-break is stated here, before collection, precisely
so it cannot be chosen later to suit an outcome.

## 3. What "independent families" means

The declaration says *"sources가 서로 독립 계열"* without enumerating them. The
repo already has exactly one such enumeration, and this pre-registration adopts
it rather than inventing a second: `config/trading_policy.yaml`
`buy.support_reserve_net.independent_support_source_families` =
`[fib, bb_lower, volume_profile]`, with
`independent_support_source_count_min: 2`.

Consequence worth stating plainly: **variant B is not a new invention — it is
the existing `buy.support_reserve_net` support contract lifted into the regular
lane.** That tier already ships `support_strength_min: moderate` +
`independent_support_source_count_min: 2` + those three families, and is today
reachable only when the regular gate failed on RSI
(`eligible_only_when_regular_gate_failure: RSI_ONLY`). The hypothesis is that
the same support contract is adequate in the regular lane too.

If a later reading of "independent families" differs from that list, it is a
**different experiment** and needs its own pre-registration.

## 4. Where the live "strong" requirement actually lives

Recorded because it is not where a reader would guess: the regular-discovery
strong-support requirement is **not** in `config/trading_policy.yaml`. It is in
`docs/playbooks/trading-decision-playbook.md` screening step 3 —
*"RSI < `screen.rsi_max` + **strong support** within `screen.support_within_pct`
…"*. For the crypto arm it is
`buy.winner_pullback_add.tiers[0].conditions.fresh_strong_support_above_avg_cost: true`.

`_FanoutGates` in `app/mcp_server/tooling/buy_candidate_fanout.py` pins
`support_strength_min == "moderate"` from `buy.support_reserve_net` — that is
the reserve-net tier's literal, **not** the regular-lane strong requirement, and
the two must not be conflated when implementing variant A.

## 5. 🔴 Divergence from the ROB-1301 spec frozen in code

`app/services/buy_gate_ab_shadow/spec.py::PRE_REGISTRATION`
(`PINNED_SPEC_SHA256 = a2814c87…`) declares a **different** experiment. Both are
called "the A/B shadow" in prose; they are not interchangeable.

| Item | ROB-1301 (pinned in code, today) | §7-1 (this document) |
| --- | --- | --- |
| Variant B | `support_strength_min: "moderate"` — strength only | moderate **AND** `source_count>=2` **AND** independent families |
| `only_difference` | `"support_strength_min"` | the composite support clause |
| Markets | `["kr", "us"]`; crypto explicitly out of scope | KR/US regular discovery **+ crypto `winner_pullback_add`** |
| Windows | `[5, 20]` trading days | D+1, **D+5**, **D+20** (adds D+1) |
| Recorded metrics | `simple_return_to_close`, `max_drawdown_from_entry_close_peak` (+ window high/low as sensitivity) | + **MFE / MAE** named as recorded fields |
| Sample target | none stated | **n ≥ 40** B-only |
| Promotion rule | none; `winner_declaration: "forbidden"` | **numeric, pre-committed** (§2) |
| Collection days | 28 | 28 (2026-08-22 → 2026-09-19) — agrees |

**Nothing in the code was changed to match this document, deliberately.**
ROB-1301's own rule is that an amendment must be approved and dated *before* the
pin is moved, and that rows already collected keep the `spec_sha256` they were
collected under. This file is that dated amendment text. Moving
`PINNED_SPEC_SHA256` is a separate, later act — see §7.

## 6. Forbidden (carried unchanged from ROB-1301)

* shadow가 제안·주문·워치로 승격 금지(순수 기록)
* 라이브 게이트 문언 무접촉
* 채점 전 중간값으로 정책 변경 논거 삼지 않기(사전 등록 원칙)

Additionally, for this registration:

* No scheduler, cron, TaskIQ, or Prefect registration.
* No mock account used as a consolation execution (1 account = 1 strategy).
* Scoring runs **once**, after the window closes. No peeking, no extension
  after a peek, no dropping names after seeing outcomes.

## 7. 🔴 Collection start conditions

Collection is **not** started by registering this document. It starts only when
every blocking condition below is satisfied, and each is objectively checkable.

### 7.1 Blocking — all markets

| # | Condition | State at registration | Owner |
| --- | --- | --- | --- |
| B1 | ROB-1301 `PRE_REGISTRATION` amended to §2's variant-B definition, promotion rule, windows, and scope; `PINNED_SPEC_SHA256` re-pinned in the same change | ❌ not done — code still declares the §5 left column | operator approves amendment → implementation |
| B2 | `CandidateEvidence` accepts support **source-count and family** evidence | ❌ not supported — `CANDIDATE_KEYS` is `{symbol, market, current_price, support_strength, support_distance_pct, rsi, honest_upside_pct, other_gate_bits}`; there is no source-count field | implementation |
| B3 | Variant A implemented as the **regular-lane strong** requirement (§4), not the reserve-net moderate literal | ❌ not verified | implementation |
| B4 | Rows carry `spec_sha256` of the **amended** spec, and pre-amendment rows are excluded from this cohort | ❌ blocked by B1 | implementation |

🔴 **B2 fails loudly, not silently.** Since §138차 ③, `evaluate_buy_gate_ab_shadow`
rejects unknown candidate keys. A session that starts sending
`support_source_count` today gets an explicit `EvaluationError` naming the
accepted keys — it will **not** quietly drop the field and record a
meaningless cohort. That is the intended behaviour and it is why B2 cannot be
skipped by "just sending it anyway".

### 7.2 Blocking — US arm only

| # | Condition | State at registration | Owner |
| --- | --- | --- | --- |
| U1 | The §5-1 US `overhang` gap is closed — an approved option from `docs/superpowers/specs/2026-08-22-us-overhang-gate-design.md` is implemented | ❌ awaiting operator decision | operator |

This is the declaration's own `전제 조건`. Until U1 is met, **US contributes
zero samples.** Measured precedent: on 2026-08-21 the valid US call returned
`n=7 · a_and_b 0 · b_only 0 · neither 7`, with CRH and UPST failing on
`overhang` alone. Setting `overhang=true` to unblock is forbidden (§6).

### 7.3 Blocking — crypto arm only

| # | Condition | State at registration | Owner |
| --- | --- | --- | --- |
| C1 | `winner_pullback_add` shadow evaluation exists for crypto | ❌ not implemented — ROB-1301's evaluator rejects any market other than `kr`/`us` (`ALLOWED_MARKETS`) | implementation |
| C2 | Crypto entry/scoring semantics defined (24/7 sessions have no "trading day", so D+1/D+5/D+20 need an explicit definition) | ❌ undefined | operator + implementation |

🔴 **C2 is a real gap in the declaration, not an implementation detail.**
"D+5/D+20 trading days" is unambiguous for KR/US and undefined for a 24/7
market. Whichever convention is chosen (UTC calendar days vs. exchange
sessions) must be written down **before** the first crypto row, or the crypto
arm is unscoreable. Recommend UTC calendar days for consistency with the
existing crypto bar semantics, but this session does not get to decide it
unilaterally — it changes what the numbers mean.

### 7.4 Non-blocking but pre-recorded

- **Feasibility risk on `n ≥ 40`.** The preceding window produced **1**
  `shadow`-family row in `review.trade_forecasts` across all markets. Reaching
  40 B-only rows in 28 days requires roughly an order-of-magnitude increase in
  recorded shadow evaluations. If the window closes short of 40, the
  pre-committed rule says **reject**, and extending the window after seeing
  that count is forbidden (§6). Recording this now so that a short sample is
  read as a real outcome rather than an excuse.
- **Clock.** The window is stated as 2026-08-22 → 2026-09-19. If B1–B4 are not
  cleared on 2026-08-22, the window start moves to the first day all blocking
  conditions are met, and the end moves with it (28 calendar days). The
  **duration** is frozen; the start date is not, because a window that runs
  while collection is impossible is not a 4-week collection.

### 7.5 Start checklist (all must be ✅ before the first row)

```
[ ] B1 spec amended + re-pinned, amendment dated
[ ] B2 source-count / family evidence accepted by the evaluator
[ ] B3 variant A = regular-lane strong requirement
[ ] B4 rows stamped with the amended spec_sha256
[ ] U1 US overhang option approved + implemented      (US arm only)
[ ] C1 crypto winner_pullback_add shadow path          (crypto arm only)
[ ] C2 crypto D+N convention written down              (crypto arm only)
[ ] window start date recorded here once all of the above are ✅
```

Per-arm start is permitted: KR may begin on B1–B4 alone, with US and crypto
joining when their own conditions clear. Each arm's start date is recorded
separately, and arms are **not** pooled across different start dates without
saying so in the scoring report.

## 8. Scoring

Formulas, single `scoring_as_of`, sensitivity separation, and the "do not use
`forecast_resolve` Brier as the experiment score" rule are unchanged from
`docs/runbooks/buy-gate-ab-shadow.md`. This document adds only the pre-committed
promotion rule in §2, which the runbook's report shape does not currently carry.

## 9. Amendment log

| Date | Change | By |
| --- | --- | --- |
| 2026-08-22 | Initial registration (retro §7-1 verbatim; confirmed final via claude-mock relay §139차 ②) | claude (구현 세션) |

Later changes require a **new dated file** that supersedes this one. See
`docs/preregistrations/README.md`.

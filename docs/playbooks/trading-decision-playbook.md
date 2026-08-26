# Trading decision playbook — as-is baseline (ROB-643)

**Status:** as-is baseline (descriptive, not prescriptive-yet). Captures the
procedure the live Claude session actually ran over 2026-06-19 → 2026-07-02.
**Purpose:** the first step toward reproducibility. Today the frame lives only
in prompt/context, so the same prompt drifts in direction and outcome across
sessions and models. This document names the frame so it can later be moved into
MCP-side tools and policy (ROB-649 `route_request`, ROB-646 trading policy YAML).

**This is a procedure contract, not an operator instruction.** It describes the
_shape_ of the decision flow — which tools run in which order, which gates apply,
which policy keys govern thresholds. It contains **no** account numbers, balances,
asset size, credentials, or routing secrets. See
[`docs/invest/report-workflows/README.md`](../invest/report-workflows/README.md#procedure-contract-vs-operator-instruction)
for the procedure-contract vs. operator-instruction boundary.

## How to read this document

- **Prose (§0–§5)** is the human-readable as-is baseline. It references
  **policy keys** (e.g. `screen.rsi_max`) instead of restating magic numbers.
- **Machine-readable blocks** (fenced ` ```yaml `) are the canonical source:
  - `lanes:` — the per-lane standard tool sequence and gates. This is the
    lane-definition source for **ROB-649** (`route_request`).
  - `policy_keys:` — every threshold captured **once**, as of 2026-07-02. This
    is the initial-value capture source for **ROB-646**. Once the ROB-646
    `trading_policy.yaml` lands, **it becomes the single authoritative source**;
    do not add new readers of the numbers below — read the policy YAML instead.
- **Thresholds are not hardcoded in the prose.** Numbers appear in exactly one
  place — the `policy_keys:` block — to avoid double maintenance. The repo is
  public; exposing these captured values is an accepted decision.
- A CI test (`tests/test_playbook_tool_names.py`) parses the `lanes:` blocks and
  fails if any `tool:` no longer exists in the DEFAULT MCP profile — this keeps
  the playbook from drifting away from the live tool registry.

> **Market-aware execution divergence (ROB-658).** The `lanes:` execution steps
> below are **KR-centric** — they name `toss_place_order` / `kis_live_place_order`
> because the captured baseline ran on KR equities. These are not static across
> markets: for **crypto/US** those KR tools are unregistered, so `route_request`
> replaces the KR place step with the generic **`place_order`** execution tool at
> runtime (`MARKET_EXECUTION_TOOLS` in
> `app/mcp_server/tooling/route_request_lanes.py`). The market→execution-tool
> mapping is therefore intentionally **not** encoded as a per-market lane step in
> the YAML (it would be one lane block per market); the `lanes:` blocks stay the
> single source for the KR sequence, and the market substitution is a documented,
> test-asserted (`tests/test_route_request.py`) runtime behavior — not drift.

All tool names below are registered in the **DEFAULT** MCP profile.

> **ROB-1239 pointer.** The canonical statement of what "route_request is
> advisory" means (below, e.g. §1 step 5, §2 step 6) is the `route_request`
> tool `description=` string in
> `app/mcp_server/tooling/route_request_registration.py`.

---

## 0) Common frame (precedes every decision)

- **Recovery gate (4 conditions):** ① US 1–2 sessions green ② foreign net-buy
  turns positive ③ VKOSPI rolls over ④ price base (higher low). Deploy reserve
  only when at least `recovery_gate.min_conditions_met` are satisfied; otherwise
  support-line conditional entries only.
- **Account routing:** buys prefer Toss (zero fee); KIS deposit cannot be
  withdrawn, so it is spent down inside KIS (single-conviction concentration,
  avoid scattering scraps). Sells execute from the holding account.
- **Hard constraints:** loss guard (sell price ≥ average × `sell.loss_guard_min_multiple`),
  KRX tick rounding, DAY order expiry at `order.day_expiry_kst` → re-place next
  day, and no two-sided (buy+sell) resting orders on the same Toss symbol.
- **Portfolio policy:** add-not-cut (average down instead of stop-loss),
  projected sector concentration at `portfolio.sector_cluster_cap_pct`
  (financials, shipbuilding/defense, bio, semis-memory) is an advisory that
  must be surfaced and recorded, never an admission block or hold reason; and
  `portfolio.max_symbols_per_theme` symbol per theme remains separate.

### Session bootstrap (run first, every session)

1. `get_operating_briefing` — one call surfaces holdings, pending orders, latest
   report summary, recent `session_context`, and **`analysis_artifacts`**
   (ROB-637: metadata-only list of recent valid analysis; bodies fetched on
   demand). This is how a new session learns what already happened.
2. `session_context_get_recent` — read yesterday's decision journal before
   comparing it with today's candidate tournament.
3. `analysis_artifact_list` — enumerate reusable prior analysis (screening
   rankings, sell verdicts). Fetch a body with `analysis_artifact_get` only when
   a specific artifact is needed — this removes duplicate re-analysis across
   sessions.
4. `get_market_index` (+ `get_fx_rate`) — load market regime and FX.

```yaml
# playbook-machine-readable: bootstrap lane (ROB-649 source)
lanes:
  bootstrap:
    intent: session start / context load
    steps:
      - tool: get_operating_briefing
        note: >-
          surfaces holdings, pending orders, latest_report, session_context, and
          analysis_artifacts (ROB-637, metadata-only)
      - tool: session_context_get_recent
        note: yesterday's decision journal
      - tool: analysis_artifact_list
        note: reusable prior analysis; fetch bodies via analysis_artifact_get on demand
      - tool: analysis_artifact_get
        note: on-demand body fetch for a specific artifact
      - tool: get_market_index
      - tool: get_fx_rate
```

---

## 1) Buy pipeline

1. `get_operating_briefing` + `get_market_index` (+ `get_fx_rate`) — load regime
   and the prior session's decisions.
2. `analyze_stock_batch(symbols ≤ 10, quick=True)` — RSI and support/resistance
   (Bollinger band, fib, volume profile) from the last CLOSED daily candle
   (`current_price` is stale, not live — call `get_quote` for a live price).
   `include_position` is accepted for compatibility but has no effect for any
   `quick` value — `analyze_stock_batch` never attaches a `position` field;
   use `get_holdings` for per-account positions. Consensus/recommendation
   require `quick=False`.
3. `get_intraday_investor_flow(symbol)` — the foreign-flow gate (today's slot +
   the confirmed multi-day net-buy history / foreign-holding burn-down embedded
   by ROB-626/640).
4. **Support selection:** prefer the **confluence** of BB-lower / fib-0 / value
   area low; place a deep limit `buy.deep_limit_pct_range` below the current
   price (pull-back catch, never chase). On crash days add a deeper rung (e.g.
   fib-50).
5. Create the order intent with `order_proposal_create(action="place")`.
   The `proposal-led-v1` route contract requires a **human Telegram approval
   click**. The operator must not call a broker preview or place tool directly:
   fresh preview/revalidation and broker submit belong to the proposal approval
   subsystem. `route_request` is advisory, so it cannot override a deployment's
   auto-approval configuration; the create response's `approval_dispatch` is
   the runtime truth. (ROB-1239: canonical semantics for "advisory" — the
   `route_request` tool `description=` string in
   `app/mcp_server/tooling/route_request_registration.py`.)
6. Broker acceptance/resting is not a fill. Converge fill/cancel state through
   the registered broker/account reconcile helper and broker evidence; the
   route has no `account_mode`, so it does not choose one reconcile tool.
7. Foreign-cascade names (e.g. semis): no market order until **price band
   reached AND foreign selling stops** — until both, only a small deep rung.
8. **Negative-class recording (ROB-712):** every reviewed-but-rejected candidate
   leaves a `decision_bucket=deferred_no_action` item with `confidence` +
   rejection reason, plus a resolvable `forecast_save(kind="price_target",
   outcome_rule_version="window-touch-v1-high-gte-low-lte", …)`
   (e.g. "no +X% within N days") so calibration isn't censored. The
   `investment_report_create` response surfaces a `warnings` advisory when an
   item is missing `confidence`.

### 1.1 `buy.support_reserve_net` — support reserve net consumer rules (operator §45/§46)

This is a separately named consumer tier of
`decision_rules.buy.support_reserve_net`, not a rewrite of the regular discovery
or `buy.deep_limit_pct_range` path. It remains proposal-led and advisory until a
separately authorized runtime consumer exists.

1. **Eligibility and evidence:** regular discovery has precedence. A candidate is
   eligible only when all regular gates except RSI pass and the only failure (or
   missing input) is RSI; an ordinary discovery survivor stays on the existing
   deep-limit tier, and a symbol cannot receive both tiers. Require `moderate`+
   support within 8% of current price and at least two distinct families from
   `fib`, `bb_lower`, and `volume_profile` (two levels from one family still count
   as one). Honest upside is at least 40%, measured once against the
   **decision-time current price**; never recalculate it against the proposed
   limit price. Candidate count zero is not permission to relax any of those
   gates at runtime.
2. **Anchor and order form:** derive the candidate limit as
   `tick_floor(S × (1-d))`, with `d` in 5–10% below support. Its final distance
   from the decision-time current price must remain within the inclusive
   `[-15%, -5%]` band. A value outside that band is **excluded**; it is never
   clamped. The only form is one `limit` + `DAY` order (no market/GTC/multi-rung
   substitution). It may be **매일 재생성** only on the next trading session with a
   fresh policy table, never as a same-session rearm.
3. **Budget, cash, and concentration:** the whole-account pending-buy
   `required_cash` ceiling is 90%; this tier's simultaneously armed
   `required_cash` ceiling is 50%. Cap at two owned-or-open symbols per market,
   one per sector cluster, and one active order per symbol; `unknown_sector` is
   `INELIGIBLE`. Use `net_orderable = fresh broker orderable cash − not-yet-
   reached-broker pending required_cash` for the same account and currency.
   `required_cash` is preview `estimated_value + fee`, falling back only to
   `quantity × limit_price`. Missing/error broker orderable data is
   **fail-closed** for this tier. A cancel *proposal* leaves its cash reserved
   until broker terminal cancellation evidence exists. Automatic notional remains
   KRW 200,000 / USD 150; a larger amount in the existing band needs a human.
4. **Allocation priority and tie-break:** apply this exact order.
   1. 이미 active/resting인 동일 symbol을 먼저 dedupe한다.
   2. 첫 슬롯은 eligible 신규 후보에 우선 배정한다.
   3. 물타기는 R-931 재심사 PASS와 Q4의 `A_limit(10%)` 완전충족 조건을 모두 만족할 때만 두 번째 후보군에 들어간다. 시장당 물타기 심볼은 최대 1개다.
   4. 같은 intent class 안에서는 `strong > moderate`, `3-source > 2-source`, 더 큰 honest upside, 더 작은 post-fill sector 증가, 더 작은 required cash 순으로 정렬한다. 완전 동률이면 신규가 물타기보다 먼저다.
5. **Add candidate:** only an R-931 `PASS` review no older than seven days and a
   policy table no older than 36 hours may enter. Recalculate `A_limit(10%)` at
   `proposed_limit_price` with `k=0.10`; a partial `A_limit` fill is forbidden,
   as are a second add symbol in one market or a second reserve-net add fill for
   the same symbol/policy version. `A_limit<=0` (already target met) is
   **NO_ORDER**: never turn `ceil(0 / proposed_limit_price)=0` into a zero-quantity
   order. Do not inherit the ordinary crash-day
   averaging exemption. Do not reissue next-day adds until fresh cost basis,
   quantity, and `A_limit` are available, and aggregate active buys across
   accounts by beneficial owner.
6. **Confirmed-fill triage:** a confirmed broker fill takes an
   account×currency triage lock, blocks every new reserve-net submit/rearm,
   coalesces same-batch fills by `[broker_account_id, currency, market_session]`,
   then rereads positions/cash/open orders. It emits approval-gated cancel
   proposals for remaining reserve-net orders; no automatic broker cancellation
   is allowed. Cash remains reserved until a broker `CANCELLED`/terminal proof.
   `unknown_or_ambiguous_order_state = KEEP_RESERVED_AND_BLOCK`; same-session
   rearm remains false. The ROB-755 command records this as a read-only proposal
   draft and does not persist or execute it.
7. **Toss approval boundary:** until veto wiring and the two separate Toss
   acceptance tests exist, **Toss 는 승인 카드 경유** only. `toss_live` is outside
   the auto-veto-capable combination list; do not treat this policy as an
   auto-approve expansion.

#### 최종 권고 리터럴 요약

- gate: RSI-only relaxation, 2 independent support families, moderate+, support within 8%, honest upside 40% 유지.
- anchor: support 아래 5~10%, 동시에 현재가 대비 5~15% 아래; 범위 밖은 제외, clamp 금지.
- budget: global 90% hard ceiling 유지, 이 티어 armed 50%, 시장당 2종목·섹터당 1·종목당 1주문.
- sizing: 자동은 현행 20만원/$150만; 그 이상은 사람 승인. Toss는 veto wiring 전 전량 사람 승인.
- fill: confirmed fill 즉시 신규 동결, cancel proposal 승인 경유, broker terminal 전 현금 해제 금지, same-session rearm 금지.
- add: R-931 PASS, `A_limit(10%)` 완전충족, 부분 A 금지, policy version당 symbol 1회, crash-day 예외 없음.

```yaml
# playbook-machine-readable: buy lane (ROB-649 source)
lanes:
  buy:
    intent: 매수 파이프라인 (buy)
    steps:
      - tool: get_operating_briefing
      - tool: get_market_index
      - tool: get_fx_rate
      - tool: analyze_stock_batch
        args: {quick: true}
      - tool: get_intraday_investor_flow
        gate: recovery_gate
      - tool: order_proposal_create
        action: place
        approval: telegram_human_click_required
        preview_owner: proposal_revalidation
        reconcile_requirement: broker_evidence
    gates:
      - recovery_gate     # deploy reserve only when >= recovery_gate.min_conditions_met
      - loss_guard        # sell price >= avg * sell.loss_guard_min_multiple (sell-side)
      - tick_rule         # KRX tick rounding
      - day_expiry        # DAY order expires at order.day_expiry_kst -> re-place next day
      - toss_two_sided    # no buy+sell resting orders on same Toss symbol
```

---

## 2) Sell (profit-taking) pipeline

1. `toss_get_positions` — scan for in-the-money / near-breakeven
   (± `sell.breakeven_near_pct`) names.
2. `analyze_stock_batch(quick=False)` — confirm distance to resistance, RSI,
   upside (upside is not part of the quick=True default allowlist).
3. **Verdict frame:**
   - **PLACE** = in-the-money AND (resistance within `sell.resistance_near_pct`
     ∨ RSI ≥ `sell.rsi_place_min` ∨ over-concentrated sector ∨ upside <
     `sell.upside_place_max_pct` ∨ foreign outflow).
   - **WATCH** = in-the-money but resistance far (beyond
     `sell.resistance_near_pct`) and RSI < `sell.watch_rsi_max` and upside ≥
     `sell.watch_upside_min_pct` (let it run).
   - **HOLD** = underwater (loss guard unmet) ∨ just bought ∨ averaging-down main
     leg.
   - **Tie-break (ROB-751)** = when resistance-near points to PLACE but
     upside-rich points to WATCH, use
     `decision_rules.sell.trim_preplace`: RSI-confirmed resistance or
     ultra-near resistance (≤2%) permits only a small pre-placed trim ladder;
     RSI-neutral 2-6% resistance becomes a system watch. In this conflict,
     `sell.upside_place_max_pct` limits trim size rather than blocking
     pre-placement eligibility.
   - **Breakeven reserve trim (§44차)** = before the regular resistance tiers,
     consume `sell.breakeven_reserve_trim` for a lot whose P&L (current price
     versus average cost) is at or above
     `-sell.breakeven_near_pct` and whose current-price multiple is strictly
     below `sell.loss_guard_min_multiple`. Calculate the resting-limit anchor as
     `max(average_cost × sell.loss_guard_min_multiple, D7-compliant lowest
     price)`, where the D7 price is the lowest price at which **one share** has
     expected net realized gain at or above
     `sell.trim_min_expected_net_realized_gain_krw` after estimated fees and
     taxes. For this tier, that is a one-share value, not a total-trim value;
     it relies on the consumer's estimated fees and taxes and adds no fee or tax
     model. After the `max`, snap the anchor **upward (`ceil`)** to the applicable
     market tick; never floor it below the guard. Use the existing trim sizing
     rule unchanged. The resulting DAY limit is the §40 auto-submit + veto-card
     contract for this advisory tier; actual dispatch still follows the runtime
     `approval_dispatch` truth.
   - If either anchor operand cannot be calculated, register the tier's WATCH
     fallback only; do not invent a local anchor or use WATCH as a substitute
     for a calculated reserve trim. Regenerate the eligible resting DAY limit
     on **every daily rep** so a prior day's expiry never leaves the reserve net
     dead. The lower P&L boundary (exactly −2% at the current policy value) is
     included; the exact loss-guard multiple is excluded.
4. Build the sell-into-strength **split ladder** just under resistance.
   [ROB-477](https://linear.app/mgh3326/issue/ROB-477) requires a bottom-anchor
   rung; run the pure `sell_ladder_fill_preview` fill-safety check **before**
   creating a proposal, preserve the core lot, and trim over-concentrated
   sectors first when in the money.
5. Create place/cancel/replace intent only through
   `order_proposal_create`. The `proposal-led-v1` route contract requires a
   **human Telegram approval click**. If the symbol has a pending buy, first
   create a separate cancel proposal and wait for confirmed cancellation
   evidence before creating the sell proposal; never call a direct cancel or
   place tool from this lane.
6. Fresh broker preview/revalidation and submit belong to the proposal approval
   subsystem. `route_request` is advisory and cannot override auto-approval
   configuration; `approval_dispatch` in the create response is the runtime
   truth. (ROB-1239: canonical semantics for "advisory" — the `route_request`
   tool `description=` string in
   `app/mcp_server/tooling/route_request_registration.py`.) Broker
   acceptance/resting is not a fill, so converge the result with the
   registered broker/account reconcile helper and broker evidence.
7. WATCH items are recorded as conditional trigger text (e.g. "when in-the-money
   AND resistance reached, place at <price>"). Today this depends on session
   memory / journal — [ROB-637](https://linear.app/mgh3326/issue/ROB-637)
   (analysis artifacts) is the durable target.

```yaml
# playbook-machine-readable: sell lane (ROB-649 source)
lanes:
  sell:
    intent: 매도(익절) 파이프라인 (sell / profit-taking)
    steps:
      - tool: toss_get_positions
      - tool: analyze_stock_batch
        args: {quick: false}
      - policy_tier: sell.breakeven_reserve_trim
        advisory: true
        priority_source: decision_rules.sell.trim_preplace.tie_breaks.tier_priority
        trigger:
          pnl_pct_min_inclusive_negated_policy_key: sell.breakeven_near_pct
          pre_guard_average_cost_multiple_max_exclusive_policy_key: sell.loss_guard_min_multiple
        anchor:
          operator: max
          operands: [average_cost_times_loss_guard, d7_compliant_lowest_price]
          post_max_tick_snap_direction: ceil
          # §142차 (2026-08-23) — average_cost × loss_guard IS the §40차
          # break-even band edge, and that band is compared inclusively, so a
          # rung landing on it is downgraded to breakeven_band. The effective
          # anchor clears the edge by one tick; nothing else changes.
          post_max_effective_anchor:
            operator: max
            operands:
              - tick_ceil_post_max_anchor
              - first_valid_tick_strictly_above_average_cost_times_one_plus_breakeven_band
            band_policy_key: order_proposals.auto_approve.breakeven_band_pct
            band_comparison_unchanged: true
            since_policy_version: "2026-08-23.1"
            retroactive: false
        sizing: existing_trim_rule
        time_in_force: DAY
        regeneration: daily_rep
        submission_contract: section_40_auto_approve_with_veto
        watch_fallback: anchor_uncomputable_only
      - tool: sell_ladder_fill_preview  # ROB-477 bottom-anchor rung, fill-safety
      - tool: order_proposal_create
        actions: [place, cancel, replace]
        approval: telegram_human_click_required
        preview_owner: proposal_revalidation
        reconcile_requirement: broker_evidence
    verdicts: [PLACE, WATCH, HOLD]
    gates:
      - loss_guard        # sell price >= avg * sell.loss_guard_min_multiple
      - tick_rule
      - toss_two_sided
```

> **Allowed helpers (not sequenced, ROB-660).** `kis_live_get_order_history` /
> `toss_get_order_history` are allowed in this lane for cancel/fill confirmation, but
> they are read-only confirmation helpers rather than ordered steps, so they live in
> `LANE_EXTRA_ALLOWED` (`app/mcp_server/tooling/route_request_lanes.py`) — not the
> YAML sequence above.

---

## 3) New-idea discovery pipeline — the *candidate tournament*

The **candidate tournament** (also called the "trading tournament" in
`app/mcp_server/README.md` and `session_context_registration.py`) is the
recurring new-buy discovery-and-ranking round. It has no code definition yet;
**this section is its first definition.** It runs as a multi-agent workflow:

1. **Multi-source fan-out (parallel):** `screen_stocks_snapshot` /
   `get_top_stocks(losers)` / `get_momentum_candidates` + `screen_stocks` /
   rotation-sector `get_sector_peers` / value screen.
2. **Pool cleanup:** exclude held names, resting-order names, and duplicates.
3. **Screening:** RSI < `screen.rsi_max` + strong support within
   `screen.support_within_pct` + honest upside ≥ `screen.upside_min_pct` + liquid
   mid-cap + not over-concentrated + **rights-issue / overhang filter**
   (`get_disclosures` — the EcoPro BM ₩1.2T rights-issue lesson).
4. **Ranking / competition (the tournament):** compare each survivor against the
   existing net (swap decision), bonus for sector diversification, bonus for
   freshness (newly pulled back).
5. **Execute:** winners only, support-line limit, `buy.per_symbol_notional_krw_range`
   per symbol.

6. **Negative-class recording (ROB-712):** every reviewed-but-rejected candidate
   leaves a `decision_bucket=deferred_no_action` item with `confidence` +
   rejection reason, plus a resolvable `forecast_save(kind="price_target",
   outcome_rule_version="window-touch-v1-high-gte-low-lte", …)`
   (e.g. "no +X% within N days") so calibration isn't censored. The
   `investment_report_create` response surfaces a `warnings` advisory when an
   item is missing `confidence`.

```yaml
# playbook-machine-readable: discovery lane / candidate tournament (ROB-649 source)
lanes:
  discovery:
    intent: 신규 발굴 (new-idea discovery / candidate tournament)
    fan_out:            # parallel multi-source
      - tool: screen_stocks_snapshot
      - tool: get_top_stocks          # losers
      - tool: get_momentum_candidates
      - tool: screen_stocks
      - tool: get_sector_peers        # rotation sector peers
    screen:
      - tool: get_disclosures         # rights-issue / overhang filter
    rank_and_execute:
      - tool: analyze_stock_batch     # deep confirm on ranked survivors
        args: {quick: false}
      - tool: toss_place_order        # winners only, support-line limit
        confirm: true
```

---

### 3.1 🎣 이중 그물 후보 소싱 (ROB-976)

크래시데이(07-20)에 "우량주 지지선 그물 후보"를 찾으려 했으나 발굴 경로가
없었다 — `get_top_stocks(losers)`는 시총/거래대금 필터가 없어 잡주만 걸렸고,
`get_support_resistance`는 심볼 단위라 유니버스 스캔이 불가능했다. **이중
그물**은 §3 fan-out의 두 축을 급락일 우량주 발굴에 맞춰 조합하는 소비
패턴이다:

1. **그물 1 — 하락률 net**: `get_top_stocks(market="kr", ranking_type="losers", min_market_cap=..., min_turnover=...)`.
   `min_market_cap`/`min_turnover`(ROB-976)로 잡주 소음을 먼저 걷어낸
   하락률 상위 우량주 목록.
2. **그물 2 — 지지선 근접 net**: `screen_stocks_snapshot(preset="support_proximity")`.
   시총/거래대금 품질 필터를 통과한 종목을 최근접 지지선까지 거리
   (`dist_to_support_pct`) 오름차순으로 반환 — 지지선 계산은
   `get_support_resistance`와 동일 로직(fib/거래량프로파일/볼린저)을
   야간 bounded 빌더가 완료봉 OHLCV 한 프레임에 적용하고, 그 프레임의
   가격·지지선·거리를 함께 저장한다. 조회 중에는 재계산하지 않는다.
3. **교차 확인**: 두 그물의 교집합(또는 그물 2 상위 종목이 그물 1에도 뜬
   경우) = 우량주가 실제로 지지선 근처까지 눌린 상태 — §3 스크리닝
   단계(RSI/upside/rights-issue 필터)로 그대로 이어서 검증.
4. **심볼 단위 재확인**: 최종 후보는 `get_support_resistance(symbol)`로
   상위 종목만 별도 실시간 재검증 후 `get_quote`로 가격을 다시 확인
   (`support_proximity` 행 자체는 최대 1세션 stale일 수 있음 —
   screen_stocks_snapshot 공통 경고).

`support_proximity`는 KR 전용(US는 후속)이며 지지선이 없는(현재가 아래
클러스터가 없는) 종목은 결과에서 제외된다(fail-closed, fabricate 금지).

---

### 3.2 ROB-1301 buy-gate A/B shadow (observation only)

KR(우선)·US 매수 스크리닝은 **variant A(현행, strong 지지 필수)를 그대로
집행**한다. 같은 후보 스냅샷·같은 `evaluation_as_of`로 **variant B
(moderate 이상 지지) 판정을 병기**한다. 다른 게이트(RSI /
`screen.support_within_pct` / honest upside / liquid mid-cap / concentration /
overhang)는 전부 동일하다. 지지 품질만 다르다.

B만 통과하는 후보는 `evaluate_buy_gate_ab_shadow`가 돌려준
`shadow_buy` `forecast_save` kwargs 로만 기록한다 (entry=판정 시점가 박제,
가정 사이징=현행 cap×0.5, 창=5거래일/20거래일). 4주 수집이 끝나기 전에는
중간 수익률을 정책 변경 논거로 쓰지 않는다. 채점 공식·단일 `scoring_as_of`·
민감도 분리는 런북
[`docs/runbooks/buy-gate-ab-shadow.md`](../runbooks/buy-gate-ab-shadow.md).

금지 (이슈 정본, 변경 없음):

* shadow가 제안·주문·워치로 승격 금지(순수 기록)
* 라이브 게이트 문언 무접촉
* 채점 전 중간값으로 정책 변경 논거 삼지 않기(사전 등록 원칙)

이 블록은 **lane sequence가 아니다.** `lanes.buy` / `lanes.discovery` 의
집행 순서는 그대로다. mock 계좌도 쓰지 않는다 (1계좌=1전략).

```yaml
# playbook-machine-readable: ROB-1301 buy-gate A/B shadow (observation only)
# NOT a lane sequence — live buy/discovery lanes are unchanged.
shadow_experiments:
  rob-1301-buy-gate-ab:
    live_gate: variant_a_unchanged
    promote: false
    steps:
      - tool: evaluate_buy_gate_ab_shadow
        note: >-
          same candidate snapshot, same evaluation_as_of; only support
          quality differs (A=strong required, B=moderate+)
      - tool: forecast_save
        when: variant_b_only
        note: >-
          shadow_buy tagging; never order_proposal_create / place_order /
          watch create
```

---

## 4) Recording / retrospective — current state and gaps

- **Current:** `session_context_append` (decision journal, free text) +
  `analysis_artifact_save` (structured, ROB-637) + Linear (defects) +
  conversation summary.
- **Gaps:**
  1. Analysis artifact structured storage now exists (ROB-637) but is not yet the
     default habit of every lane.
  2. **No post-hoc verification (retrospective) loop** — no procedure
     automatically checks "was that verdict right?" against fills / returns.
  3. Verdict thresholds (the `policy_keys` below) live only in prompt/context
     until ROB-646 makes them a policy file — so they drift when the session or
     model changes.

---

## 5) Non-determinism diagnosis (the reproducibility target)

1. The frame/criteria live **only in prompt/context** — no tool enforces them.
2. **Tool-choice latitude** — which tools, how many times, varies per run.
3. **Data timing** (foreign-flow slot, NXT vs. regular session) changes the input
   itself.
4. Verdict thresholds are **implicit** (model discretion).

→ Direction: codify §1–§3 as MCP-side higher-order tools/policy (e.g.
`buy_plan_generate`, `sell_verdict_evaluate`, `discovery_run` / `route_request`),
make thresholds a policy file, and add a retrospective loop. Concrete design
lives in the tradingcodex-absorption issues (ROB-646 / ROB-649).

---

## Policy-key capture (ROB-646 initial values)

Captured as-of **2026-07-02** from the 2026-06-19 → 2026-07-02 live sessions.
Once ROB-646 `trading_policy.yaml` lands it is the single authoritative source;
these values are the seed, not a second source of truth.

> **Authority (ROB-646, landed):** `config/trading_policy.yaml` is now the
> single authoritative source of these values; this block is the historical
> seed. The policy governs **judgment thresholds, decision rules, and the
> sector-cluster concentration advisory only** — NOT the fail-closed code guards (loss guard,
> ladder near-market, RSI scoring bands), NOT `symbol_trade_settings` (live
> sizing), and it does not revive `trade_profile` (dead since ROB-488). Lane
> `sell` = "profit_taking" (same lane, human alias). Read it via
> `get_trading_policy(market, lane)`. Decision-rule blocks such as
> `decision_rules.sell.trim_preplace` are policy guidance that resolves
> threshold conflicts without changing execution guards.


```yaml
# playbook-machine-readable: policy_keys (ROB-646 initial-value capture)
# authoritative_source_when_landed: ROB-646 trading_policy.yaml
policy_keys:
  recovery_gate.min_conditions_met:
    lanes: [buy]
    captured: 2
    unit: count
    of: 4
    semantics: min recovery-gate conditions to deploy reserve (else support-conditional only)

  portfolio.sector_cluster_cap_pct:
    lanes: [buy, sell]
    captured: 10
    unit: percent
    semantics: projected concentration warning per sector cluster (~9-10%); advisory only
      — surface and record it, never use it as an admission block or hold reason
  portfolio.max_symbols_per_theme:
    lanes: [buy, discovery]
    captured: 1
    unit: count
    semantics: one symbol per theme

  order.day_expiry_kst:
    lanes: [buy, sell]
    captured: "20:00"
    unit: kst_time
    semantics: DAY order expiry; unfilled -> re-place next day

  buy.deep_limit_pct_range:
    lanes: [buy]
    captured: [-12, -3]
    unit: percent
    semantics: deep limit distance below current price (pull-back catch, no chasing)
  buy.per_symbol_notional_krw_range:
    lanes: [buy, discovery]
    captured: [200000, 400000]
    unit: krw
    semantics: per-symbol order sizing for new entries (policy threshold, not account balance)

  sell.loss_guard_min_multiple:
    lanes: [buy, sell]
    captured: 1.01
    unit: multiple_of_avg_cost
    semantics: minimum sell price as multiple of average cost (loss guard)
  sell.breakeven_near_pct:
    lanes: [sell]
    captured: 2
    unit: percent
    semantics: near-breakeven scan band (+/-)
  sell.resistance_near_pct:
    lanes: [sell]
    captured: 6
    unit: percent
    semantics: resistance-proximity threshold for PLACE vs WATCH
  sell.rsi_place_min:
    lanes: [sell]
    captured: 58
    unit: rsi
    semantics: RSI at/above which PLACE is favored
  sell.upside_place_max_pct:
    lanes: [sell]
    captured: 45
    unit: percent
    semantics: honest upside below which PLACE is favored
  sell.watch_rsi_max:
    lanes: [sell]
    captured: 52
    unit: rsi
    semantics: RSI below which WATCH (let-it-run) is allowed
  sell.watch_upside_min_pct:
    lanes: [sell]
    captured: 50
    unit: percent
    semantics: upside at/above which WATCH (let-it-run) is allowed

  screen.rsi_max:
    lanes: [discovery]
    captured: 45
    unit: rsi
    semantics: max RSI for a discovery candidate
  screen.support_within_pct:
    lanes: [discovery]
    captured: 8
    unit: percent
    semantics: strong support must be within this distance
  screen.upside_min_pct:
    lanes: [discovery]
    captured: 40
    unit: percent
    semantics: minimum honest upside for a candidate
```

---

## Related issues

- [ROB-637](https://linear.app/mgh3326/issue/ROB-637) — analysis artifact
  storage (save/list/get); the durable-record target for WATCH triggers and
  reusable analysis.
- [ROB-638](https://linear.app/mgh3326/issue/ROB-638) — lean `analyze_stock_batch`
  cache / delta selectors.
- [ROB-626](https://linear.app/mgh3326/issue/ROB-626) / ROB-640 — intraday
  investor-flow freshness + confirmed multi-day foreign-flow embed.
- **ROB-646** — trading policy YAML (single source for the `policy_keys` above).
- **ROB-1301** — buy-gate A/B shadow (variant B moderate+ support). Playbook
  §3.2 is observation-only; live gates stay variant A.
- **ROB-649** — `route_request` (consumes the `lanes:` blocks above).

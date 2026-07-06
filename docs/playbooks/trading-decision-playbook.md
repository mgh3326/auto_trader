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
  over-concentration cap of `portfolio.sector_cluster_cap_pct` per sector cluster
  (financials, shipbuilding/defense, bio, semis-memory), and
  `portfolio.max_symbols_per_theme` symbol per theme.

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
2. `analyze_stock_batch(symbols ≤ 10, mode=quick, include_position=true)` — RSI,
   honest consensus (upside), support/resistance (Bollinger band, fib, volume
   profile), and per-account position.
3. `get_intraday_investor_flow(symbol)` — the foreign-flow gate (today's slot +
   the confirmed multi-day net-buy history / foreign-holding burn-down embedded
   by ROB-626/640).
4. **Support selection:** prefer the **confluence** of BB-lower / fib-0 / value
   area low; place a deep limit `buy.deep_limit_pct_range` below the current
   price (pull-back catch, never chase). On crash days add a deeper rung (e.g.
   fib-50).
5. Execute: `toss_place_order(confirm=true)` (fee-free, preferred) or
   `kis_live_place_order(thesis + strategy, dry_run preview → live)` to spend
   down KIS deposit. `kis_live_place_order(dry_run=true)` is the preview; there
   is no separate KIS preview tool.
6. Foreign-cascade names (e.g. semis): no market order until **price band
   reached AND foreign selling stops** — until both, only a small deep rung.

7. **Negative-class recording (ROB-712):** every reviewed-but-rejected candidate
   leaves a `decision_bucket=deferred_no_action` item with `confidence` +
   rejection reason, plus a resolvable `forecast_save(kind="price_target", …)`
   (e.g. "no +X% within N days") so calibration isn't censored. The
   `investment_report_create` response surfaces a `warnings` advisory when an
   item is missing `confidence`.

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
        args: {mode: quick, include_position: true, max_symbols: 10}
      - tool: get_intraday_investor_flow
        gate: recovery_gate
      - tool: toss_place_order        # account routing: Toss preferred
        confirm: true
      - tool: kis_live_place_order    # KIS deposit spend-down; dry_run preview -> live
        confirm: true
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
2. `analyze_stock_batch` — confirm distance to resistance, RSI, upside.
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
4. Execute from the **holding account**: Toss holdings via `toss_place_order`, KIS
   holdings via `kis_live_place_order` (`dry_run` preview → live). Sell-into-strength
   **split ladder** just under resistance;
   [ROB-477](https://linear.app/mgh3326/issue/ROB-477) requires a bottom-anchor rung
   (`sell_ladder_fill_preview` checks fill-safety), preserve the core lot. Trim
   over-concentrated sectors first when in the money. If the name has a **pending buy
   limit on Toss**, `toss_cancel_order` **first** — no two-sided (buy+sell) resting
   orders on one symbol.
5. WATCH items are recorded as conditional trigger text (e.g. "when in-the-money
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
      - tool: toss_cancel_order       # clear same-symbol buy pending (two-sided) before sell
      - tool: toss_place_order        # sell-into-strength split ladder (Toss holdings)
        confirm: true
      - tool: kis_live_place_order    # sell KIS holdings from holding account; dry_run preview -> live
      - tool: sell_ladder_fill_preview  # ROB-477 bottom-anchor rung, fill-safety
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
   rejection reason, plus a resolvable `forecast_save(kind="price_target", …)`
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
      - tool: toss_place_order        # winners only, support-line limit
        confirm: true
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
> sector-cluster concentration cap only** — NOT the fail-closed code guards (loss guard,
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
    semantics: over-concentration cap per sector cluster (~9-10%)
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
- **ROB-649** — `route_request` (consumes the `lanes:` blocks above).

# ROB-839 — Crypto Judgment Rules Policy Promotion

## Goal

Promote the crypto judgment rules that recent operator reports applied
implicitly into `config/trading_policy.yaml`. The policy must reduce model
discretion without inventing numeric cutoffs that the reports did not define.

The change is advisory and read-only. It does not alter order execution,
loss-sale guards, ladder guards, or database state.

## Evidence

The seed is limited to these operator reports:

- `~/services/auto_trader-operator/reports/crypto-recheck-2026-07-11.md`
- `~/services/auto_trader-operator/reports/crypto-recheck-2026-07-12.md`
- `~/services/auto_trader-operator/reports/crypto-recheck-2026-07-12-opus.md`
- `~/services/auto_trader-operator/reports/crypto-recheck-2026-07-12-fable.md`

The reports explicitly support:

- a four-input recovery frame using Fear & Greed, 24-hour alt breadth, BTC
  global/top-trader long-short ratios, and BTC kimchi premium;
- `2 of 4` as the minimum recovery-gate decision rule;
- `breadth > 50%` and `BTC L/S <= 1.5` as numeric conditions;
- support/resistance selection that favors multi-source confluence, repeatedly
  using Fibonacci, value area, Bollinger lower/middle, and volume POC;
- rejection of low-liquidity rotation-pump candidates, especially when alt
  breadth is below 50%, and rejection of sharp movers without support structure
  or decision history.

The reports do not define a numeric Fear & Greed cutoff, kimchi-premium cutoff,
same-day gain cutoff, or 24-hour traded-value floor for those judgments. These
values must remain `null`; a model must not fill them by inference.

## Policy Shape

Add a typed top-level `market_rules` mapping. The crypto entry contains three
named rules, each tagged with the lanes where it is relevant:

```yaml
market_rules:
  crypto:
    recovery_gate:
      lanes: [buy]
      advisory: true
      min_conditions_met: 2
      of: 4
      conditions: [...]
    support_resistance:
      lanes: [buy, sell, discovery]
      advisory: true
      selection_rule: confluence_first_then_source_priority
      source_priority: [...]
      confluence_examples: [...]
    no_chasing:
      lanes: [buy, discovery]
      advisory: true
      daily_change_pct_threshold: null
      min_trade_value_24h_krw: null
      criteria: [...]
      follow_up: ...
```

This is separate from `market_overrides`, which continues to override scalar
threshold values. It is also separate from the existing global
`decision_rules`, whose tier/action/sizing shape is specific to decisions such
as `sell.trim_preplace` and does not fit evidence-source ranking or qualitative
criteria.

## Recovery Gate

The four conditions are complete as inputs, even when their quantitative
decision threshold is not yet defined:

1. Fear & Greed: threshold `null`; retain the report language about a recovering
   trend, but do not convert the observed value or streak into a cutoff.
2. 24-hour alt breadth: `> 50 percent`.
3. BTC long-short ratio: both global-account and top-trader-position inputs,
   `<= 1.5 ratio`.
4. BTC kimchi premium: threshold `null`; retain the report interpretation of a
   discount/no-domestic-FOMO state without deciding whether it passes.

The decision rule is `min_conditions_met: 2`, `of: 4`. A missing observation or
a condition with no quantitative threshold is not guessed. The rule documents
the advisory reserve-deployment frame; it does not enforce an order guard.

## Support and Resistance Sources

Selection is confluence-first. A level backed by multiple independent sources
outranks a single-source level. When confluence count and reported strength do
not settle the choice, use a stable source priority derived from the recurring
report combinations:

1. Fibonacci levels
2. value-area levels
3. Bollinger lower band
4. Bollinger middle band
5. volume POC

The policy also records representative report-derived combinations, including
`fib_0 + value_area_low + bb_lower`, `bb_middle + fib_23_6`, and
`bb_middle + volume_poc`. These examples explain ranking; they do not calculate
prices or replace fresh market analysis.

## No-Chasing Rule

The numeric fields intentionally remain `null`:

- `daily_change_pct_threshold`
- `min_trade_value_24h_krw`

The fixed qualitative criteria are:

- exclude low-liquidity rotation-pump candidates;
- treat new alt entries as ineligible when 24-hour alt breadth is below 50%;
- exclude sharply rising candidates that lack a support structure or decision
  history.

The YAML comment and a structured `follow_up` string state that operator
experience must establish the numeric cutoffs in a later operator PR. Until
then, consumers must cite the qualitative criteria and `null`, not substitute
their own numbers.

## Loader and Response Contract

`TradingPolicyDocument` gains strict Pydantic models for the new mapping and its
three crypto rule shapes. `extra="forbid"` remains in force at every new level.
Nullable thresholds are explicitly typed rather than represented by missing
keys.

`get_policy_for(market, lane)` adds a `market_rules` object containing only the
rules whose `lanes` include the requested lane. Existing response fields remain
unchanged:

- `market`
- `lane`
- `version`
- `content_hash`
- `thresholds`
- `decision_rules`

The MCP `get_trading_policy` wrapper continues to return the service response
with `success: true`. The existing `{version, content_hash}` stamp remains the
contract: version comes from YAML and content hash remains the first 12 hex
characters of SHA-256 over the raw YAML bytes.

The policy version becomes `2026-07-12.1`; `captured_as_of` and `source` are
updated to identify ROB-839 and the four operator reports.

## Boundaries

- All new rules are `advisory: true`.
- No execution path consumes these rules as a hard guard in this change.
- Loss-sale and ladder fail-closed guards remain code-owned and unchanged.
- Existing scalar threshold overrides remain unchanged.
- No database model or migration changes.
- No operator report or external operator workspace file is modified.

## Test Strategy

Implementation follows TDD:

1. Schema tests first:
   - shipped YAML validates;
   - all four recovery inputs and explicit/null thresholds deserialize;
   - support source priority and no-chasing nulls deserialize;
   - unknown keys in nested market-rule objects fail validation.
2. Service tests next:
   - crypto buy exposes all three applicable rules plus the version stamp;
   - crypto discovery exposes support/resistance and no-chasing, not the buy-only
     recovery gate;
   - crypto sell exposes support/resistance only;
   - non-crypto markets expose no crypto market rules;
   - existing thresholds, decision rules, overrides, and hash behavior remain
     intact.
3. MCP tests last:
   - `get_trading_policy(market="crypto", lane="buy")` exposes the filtered rules
     and `{version, content_hash}`.

Targeted tests run after each red/green cycle. Final verification runs the
relevant schema/service/MCP suites and `make lint`.


# TODOS

## Crypto-native edge families (funding / OI / liquidation) — investigate after ROB-351 families 1-3

- **What:** Evaluate funding-rate / open-interest / liquidation-shock continuation & reversal
  strategies on Binance USD-M, gated on first confirming usable historical data quality.
- **Why:** Codex outside-voice (ROB-351 eng-review) flagged that funding/OI/liquidation are
  closer to *crypto-native* edge sources than the generic trend/momentum/reversal families
  that prior campaigns (ROB-316/320/324/339/342) keep killing. Worth a dedicated look that
  ROB-351 deliberately parked.
- **Pros:** Targets a structurally different edge than the families already shown net-negative;
  funding carry in particular has documented cross-sectional signal.
- **Cons:** OI history from the API is ~30 days; liquidation history is scarce/unreliable.
  Likely blocked at `needs_more_data` unless a durable archival source is found first.
- **Context:** ROB-351 design doc `~/.gstack/projects/mgh3326-auto_trader/mgh3326-rob-351-design-*.md`
  ("Out of scope" + eng-review section). Do NOT invent weak proxies — if data is missing,
  stop with an explicit blocker. Same safety boundary as ROB-351 (research-only).
- **Depends on / blocked by:** ROB-351 families 1-3 outcome; a vetted funding/OI/liquidation
  historical data source.

## Completed

- **ROB-580**: Multi-window crypto order flow improvement (v0.2.1).


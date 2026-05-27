# ROB-321 PR3 — Signal / risk contract + supervisor (dry-run)

> Sibling of `docs/plans/ROB-321-kis-mock-scalping-loop-plan.md` (executes PR3).

**Goal:** The deterministic decision layer of the KIS mock scalping loop — a pure
signal + risk contract and an event-driven supervisor that turns the PR2 quote
stream into `TriggerEvent`s. **Dry-run only**: no orders, no ledger, no risk
re-check side effects. Edge-agnostic plumbing (ROB-316: scalping is net-negative
after fees; v1 strategy is an intentional toy).

**Architecture:** Mirror the Binance `demo_scalping` pattern, KIS-specific:
cash-equity **long-only**, KRW notional, and a no-chase guard. The supervisor
builds 1-minute candles from trade ticks (KIS quote WS has no klines), re-runs
the signal on each close, and emits a `TriggerEvent` gated on a fresh orderbook
quote + per-symbol debounce. Order execution + ledger risk re-check are PR4.

---

## Delivered (all committed, 62 tests green, ruff/format/ty clean)

### Pure cores — `app/services/brokers/kis/mock_scalping/`
- **`contract.py`** — `ScalpingRiskLimits` (KRW; allowlist, max_notional, max
  open positions, daily order/loss caps, cooldown, spread + freshness gates),
  `ReasonCode` (append-only string constants), `LedgerSnapshot`,
  `MarketConditions`, `RiskDecision`, and `evaluate_risk()` which **accumulates
  every blocking reason** (no short-circuit). Long-only: a `SELL` entry yields
  `SHORT_ENTRY_NOT_ALLOWED`.
- **`signal.py`** — `Candle`, `SignalConfig`, `SignalDecision`,
  `evaluate_signal()`: long-only SMA-trend + prior-high breakout, **no-chase
  guard** (`CHASE_TOO_FAR` when the breakout already ran past `max_chase_bps` —
  ROB-321 §3 "당일 급등주 추격 금지"), fixed-bps TP/SL.
- **`order_intent.py`** — `OrderIntent` (BUY-only, KRW notional pinned to the
  risk cap) + `build_order_intent()` (None for non-entry/SELL) + JSON evidence.

### Event supervisor — `app/services/brokers/kis/mock_scalping_ws/`
- **`candles.py`** — `CandleAggregator`: tick → 1-minute OHLC, closes a candle
  when the first tick of a later minute arrives (pure, injected clock).
- **`supervisor.py`** — `KisScalpingSupervisor`: consumes a `QuoteTick |
  OrderBookSnapshot` source; orderbook → state, tick → candle; on close →
  `evaluate_signal` → `TriggerEvent` gated on fresh orderbook (`book_age <=
  max`) + per-symbol debounce. **No risk re-check / order / ledger** —
  `on_trigger` is caller-supplied (PR4 wires the confirm-gated executor).
- **`state.py`** — added `book_age_seconds()` (book-specific freshness) so a
  stale bid/ask trips the gate even while trade ticks arrive.

### Gating
- The PR2 `kis_mock_scalping_ws_enabled` flag (default off) gates any daemon.
- The PR2 AST import guard already forbids `mock_scalping_ws/` (now incl.
  supervisor + candles) from importing any order/ledger/execution module — still
  green, keeping the read-only boundary structural.

---

## Deferred to PR4 (not in this PR)
- **Dry-run daemon entrypoint** wiring the real `KISQuoteWebSocket`
  (callback → async-queue adapter) → supervisor → log-only `on_trigger`. Lands
  with PR4 because that is where `on_trigger` becomes the confirm-gated mock
  executor + ledger risk re-check; a log-only variant would be throwaway glue.
- Exec bridge, round-trip ledger/reconcile, smoke runbook (PR4 proper). PR4 also
  wires PR1's `ScalpingExitContext` for the stop-loss exit.

---

## Self-review
- **Spec coverage (master plan PR3):** EntrySignal/ScalpingStrategy contract
  (signal.py), risk envelope incl. max notional / open positions / cooldown /
  daily caps (contract.py), supervisor candle→signal→trigger with freshness +
  debounce (supervisor.py). ✅
- **Long-only correctness:** signal never emits SELL; risk blocks SELL entry;
  build_order_intent returns None for SELL — three independent guards.
- **Determinism:** all decision logic is pure with injected clocks; tests pin
  breakout, no-chase, every risk reason code, candle rollover, trigger gating
  (fresh-book / stale-book / debounce / unknown-symbol).

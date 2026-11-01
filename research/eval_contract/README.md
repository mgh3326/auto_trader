# Evaluation contract v1 (ROB-1433, plan unit U5)

One evaluation shape for every market, with an adapter per market that wraps the
existing harness. Research-only: no broker, account, database session, scheduler
or in-process LLM import; nothing here writes to `execution_ledger` or any table.

## Shape

```
Decision ─> Intent ─> OrderSpec ─> FillRow ─> Outcome (R_h, h = 1/5/20)
                                      └─> FillModelResult ─> EvaluationReport
```

| Type | Meaning |
|---|---|
| `Decision` | what was decided and when; `data_cutoff <= decided_at` or it is rejected (look-ahead). A result's chain must run forward in time: decision ≤ order submit ≤ fill ≤ order expiry ≤ intent deadline |
| `Intent` | side, notional cap, deadline |
| `OrderSpec` | one DAY limit child order (v1 knows only DAY limits); `origin` is `strategy` or `engine_time_trim` |
| `FillRow` | a simulated fill named like the `execution_ledger` columns (test-checked); always `broker=simulator`, `account_mode=backtest`, `source=simulated:<model>` |
| `CostModel` | buy fee, sell fee, sell tax |
| `Outcome` | markout of a buy fill `h` sessions after the fill session: gross, net of costs, cash, net − cash; past the data it is censored (no values), never 0 |
| `FillModelResult` | one fill model's own decision path, orders, fills, outcomes, terminal value and cash baseline. `net_vs_cash` is the primary metric; `fill_rate` is a diagnostic. `status="unavailable"` carries a reason and no numbers |
| `Provenance` | code ref, data-manifest sha256, config sha256, registry ref |
| `RegistryRef` | hypothesis id, registry sha256, cumulative test count `k >= 1` |
| `EvaluationReport` | must reference a registry entry and carry both bracket ends |

Hashes are sha256 over canonical JSON (sorted keys, `Decimal` as text, floats refused).

## Fill models

| Model | Window | Rule |
|---|---|---|
| `optimistic_touch` | every bar overlapping the order life | low ≤ limit (buy) |
| `conservative_through` | bars wholly inside the order life | low ≤ limit − 1 tick |
| `volume_share` | optimistic window | touch, capped at `participation × window volume`; no volume → unavailable |
| `empirical` | — | fit hook only; `fit()` reports `insufficient_sample` below 30 observations and `fit_not_implemented` above; `fill()` always refuses |

Both bracket ends fill a gap at the first reachable price (e.g. a daily open at or
through the limit). The report prints the conservative and optimistic ends side by side.

## KR adapter (`adapters/kr_d3.py`)

Wraps `research.kr_corpus.d3_engine.PortfolioEngine`:

- `run_engine(run_input, ticks, None)` is the legacy engine, untouched.
- With a fill model, a subclass overrides only `_buy_fill_price` / `_sell_fill_price`;
  sizing, ranking, cash, expiry and fees stay the engine's. On daily bars
  `optimistic_touch` equals the engine's own rule (tested for all four arms).
- `evaluate(...)` builds an `EvaluationReport` for the legacy engine plus every fill model.

v1 limits:
- Only full-or-nothing models run through the engine hooks: `volume_share` is
  unavailable (D3 bars carry no volume) and `empirical` is unfitted.
- C3 time trims fill at the open under every model, because the engine runs them
  outside the hooks. They appear as sell orders with `origin="engine_time_trim"`.
- Net vs cash needs `CashflowView.NO_CONTRIBUTION` (equal capital). The cash rate
  is 0: D3's NAV earns no interest on idle cash, so an interest-bearing baseline
  would bias the comparison.
- Markouts count `market_sessions` (bars are only looked up for closes), not bars. A horizon that lands on a missing bar
  is censored.
- Simulated fills use `venue="krx"`, as the KIS KR ledger normalizer does.

## Registry

`RegistryRef` is a shape-only stub until U1's hypothesis registry is merged
(`TODO(U1)` in `contract.py`). A report without a registry reference is
rejected at construction (`Provenance`), by the header check any future loader must
call first (`EvaluationReport.check_registry_header`; v1 has no loader), and at print
(`render_report` re-validates the hash, `k` and hypothesis id).

## Tests

`tests/research/eval_contract/` (no DB, no network):

- `test_eval_contract_rob1383_q2.py` — the ROB-1383 Q2 bracket: optimistic 5/5,
  conservative 0/5 on a deterministic mirror of the five filled orders (the real
  rows are private); the legacy `q2_q3_orders.run` and the contract agree order by order.
- `test_eval_contract_kr_d3.py` — legacy D3 results reproduced exactly (events,
  fills, metrics, pinned `terminal_nav`), deterministic reports, bracket separation.
- `test_eval_contract_types.py` — ledger column shape, look-ahead, censoring,
  registry rejection, fill models, report section order.

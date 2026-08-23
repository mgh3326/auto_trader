# ROB-1301 — buy-gate A/B historical backtest (research-only)

Re-applies the **pre-registered** ROB-1301 gate and scoring code to frozen
historical bar corpora, to get a prior-sample read on the same question the
live 4-week shadow is measuring: *does requiring `strong` support (variant A)
reject candidates that would have been fine under `moderate+` (variant B)?*

🔴 This does **not** replace the live shadow collection, and it is **not** a
policy input until it has been through an adversarial verification round.

## What is reused, not reinvented

| Input | Authority |
|---|---|
| A/B verdict + cohort | `app.services.buy_gate_ab_shadow.evaluate.evaluate_candidate` |
| per-sample return / drawdown | `app.services.buy_gate_ab_shadow.scoring.score_window` |
| support levels + strength | live `get_support_resistance_impl` (preloaded frame) |
| RSI(14) | live `_compute_indicators` |

`app/` is read-only here: this package imports it and changes nothing.

## Layout

- `preregistration.py` — the frozen addendum (corpus, sampling, universe,
  which gates cannot be reconstructed). Digest-pinned; a change fails a test.
- `corpus.py` — parquet loaders. Refuses any path under a sealed `holdout/`.
- `reconstruct.py` — point-in-time evidence for one (symbol, decision session).
- `run_backtest.py` — driver + aggregation.
- `tests/` — freeze, look-ahead, cohort-neutrality, router guards.

## Run

```bash
uv run python -m research.buy_gate_ab_backtest.run_backtest \
  --market kr --out /path/to/kr.json
```

Markets: `kr`, `us`, `crypto_upbit_krw`, `crypto_binance_usdt_spot`.

No network, no operating DB, no broker. The app `Settings` singleton is
satisfied with inert placeholders set in `reconstruct.py` before the first app
import, so no operating credential is ever loaded.

## Known limits (read these before citing a number)

1. **Sealed holdout untouched.** 2025-01-01..2026-07-31 is not read for any
   market, so nothing here covers the recent regime.
2. **Three shared gates are neutralised** (`honest_upside_pct >= 40`,
   `liquid_midcap`, `concentration`, `overhang`) because no bar corpus holds
   their inputs. They are neutralised *identically for A and B*, so the A-vs-B
   contrast survives, but every absolute admit count is an upper bound.
3. **US corpus is survivorship-biased** — delisted names are absent entirely.
4. **Support strength is a proxy** in the sense that it is recomputed from
   60 daily bars rather than read from a live session's analysis payload, and
   the live US intraday-price override is disabled.
5. **Crypto is outside the upstream spec's markets** and is reported as an
   annex, never merged into the KR/US tables.

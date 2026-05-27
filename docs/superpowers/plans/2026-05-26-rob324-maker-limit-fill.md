# ROB-324 Maker/Limit-Fill Edge Re-evaluation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-evaluate whether a conservative maker/limit-fill execution model recovers a net-after-cost edge for the `meanrev_zscore_fade` candidate (XRPUSDT + BTCUSDT) that the taker model couldn't — producing an honest `validated`/`not_validated`/`insufficient_data` verdict and an auditable artifact.

**Architecture:** Real tick-level limit-fill re-simulation in NautilusTrader (limit entry filled against trade ticks, 1-bar cancel = missed fill, maker-limit TP, taker-stop SL) plus a pure deterministic conservative overlay (queue-loss drop + adverse-selection cost). Three scenarios — realistic taker (4 bps), data-derived maker, conservative maker — are fed to the **unchanged** `validated_gate.evaluate_gate`. Research/backtest only; no broker, order, scheduler, prod, or runtime side effects.

**Tech Stack:** Python 3.13, NautilusTrader 1.227.0 (rob-320 research venv), pytest 9 (main `uv` env for pure tests), Binance public trade-tick parquet catalog (read-only, from the rob-320 worktree).

**Design spec:** `docs/superpowers/specs/2026-05-26-rob324-maker-limit-fill-design.md`

---

## Conventions used throughout

- **Repo root (this worktree):** `/Users/mgh3326/work/auto_trader.rob-324`
- **Research dir:** `<repo>/research/nautilus_scalping` — most commands `cd` here.
- **Nautilus venv (runs backtests):** `/Users/mgh3326/work/auto_trader.rob-320/research/nautilus_scalping/.venv/bin/python` — referenced below as `$NVENV`. Set once per shell:
  ```bash
  export NVENV=/Users/mgh3326/work/auto_trader.rob-320/research/nautilus_scalping/.venv/bin/python
  export CATALOG=/Users/mgh3326/work/auto_trader.rob-320/research/nautilus_scalping/catalog
  ```
- **Pure tests** (no nautilus) run from repo root with `uv run pytest research/nautilus_scalping/tests/<file> -v`.
- **Result artifacts** live under `results/` which is gitignored — curated artifacts are force-added (`git add -f`), exactly as `results/rob320/meanrev.json` was.
- **Commit co-author line:** `Co-Authored-By: Paperclip <noreply@paperclip.ing>` (project convention).

## File structure

| File | Create/Modify | Responsibility |
|------|---------------|----------------|
| `research/nautilus_scalping/results/rob324/binance_usdm_commission_rates.json` | Create (copy) | Fee provenance input (force-added). |
| `research/nautilus_scalping/maker_fill.py` | Create | **Pure** scenario builders + conservative overlay. No nautilus import. |
| `research/nautilus_scalping/tests/test_maker_fill.py` | Create | Pure tests: missed-fill, queue-loss, adverse cost, determinism, verdict vocab. |
| `research/nautilus_scalping/strategy_meanrev.py` | Modify | Add `execution_mode="maker"` branch (limit entry, timeout, maker TP, taker SL, records). Default `"taker"` unchanged. |
| `research/nautilus_scalping/backtest_runner.py` | Modify | Maker mode: real-fee instrument override + richer records + counters. |
| `research/nautilus_scalping/validate_maker_fill.py` | Create | Driver: 3 scenarios → `evaluate_gate` → write v2 artifact. |
| `research/nautilus_scalping/results/rob324/maker_fill.json` | Create (generated) | The auditable result (force-added). |

---

## Task 1: Branch setup + catalog wiring smoke

**Files:**
- Create: `research/nautilus_scalping/results/rob324/binance_usdm_commission_rates.json` (copied)

- [ ] **Step 1: Copy the commission artifact into this branch**

```bash
cd /Users/mgh3326/work/auto_trader.rob-324/research/nautilus_scalping
mkdir -p results/rob324
cp /Users/mgh3326/work/auto_trader/research/nautilus_scalping/results/rob324/binance_usdm_commission_rates.json results/rob324/
```

- [ ] **Step 2: Verify the venv + catalog are reachable and the existing taker pipeline still runs**

This reproduces a small slice of ROB-320 to prove `$NVENV` + `$CATALOG` work from this worktree before building anything.

```bash
export NVENV=/Users/mgh3326/work/auto_trader.rob-320/research/nautilus_scalping/.venv/bin/python
export CATALOG=/Users/mgh3326/work/auto_trader.rob-320/research/nautilus_scalping/catalog
cd /Users/mgh3326/work/auto_trader.rob-324/research/nautilus_scalping
PYTHONPATH=../.. $NVENV -c "
from pathlib import Path
from backtest_runner import run
trades = run(Path('$CATALOG'), 'XRPUSDT', 'meanrev_zscore_fade',
             {'lookback':20,'z_entry':'2.0','tp_bps':30,'sl_bps':30}, '100')
print('XRPUSDT taker trades:', len(trades))
assert len(trades) > 50, 'expected a few hundred trades'
print('OK: catalog + venv wired')
"
```
Expected: prints a trade count (a few hundred) and `OK: catalog + venv wired`.

- [ ] **Step 3: Commit setup**

```bash
cd /Users/mgh3326/work/auto_trader.rob-324
git add -f research/nautilus_scalping/results/rob324/binance_usdm_commission_rates.json
git commit -m "chore(rob-324): vendor Binance USDM Demo commission artifact into branch

Force-added past results/ gitignore (mirrors results/rob320/meanrev.json).
Fee provenance input for the maker/limit-fill re-evaluation. Read-only;
contains no secrets, signatures, balances, positions, or account ids.

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 2: `maker_fill.py` — module skeleton + constants

**Files:**
- Create: `research/nautilus_scalping/maker_fill.py`
- Test: `research/nautilus_scalping/tests/test_maker_fill.py`

- [ ] **Step 1: Write the failing test**

```python
# research/nautilus_scalping/tests/test_maker_fill.py
"""ROB-324 — pure maker/limit-fill scenario builders.

Records in, validated_gate.Trade lists out. No nautilus; fully deterministic."""
from __future__ import annotations

from maker_fill import (
    MAKER_FEE_BPS,
    TAKER_BASELINE_BPS,
    MakerTradeRecord,
)


def test_module_constants_match_demo_fees() -> None:
    assert MAKER_FEE_BPS == 2.0
    assert TAKER_BASELINE_BPS == 4.0


def _rec(net, comm, notional, ts, *, filled=True, tp_hit=True, adverse=0.0) -> MakerTradeRecord:
    return MakerTradeRecord(
        net_at_real_fees=net, commission_real=comm, notional=notional,
        ts_opened=ts, filled=filled, tp_hit=tp_hit, adverse_excursion_bps=adverse,
    )


def test_record_is_frozen_dataclass() -> None:
    r = _rec(1.0, 0.04, 100.0, 0)
    assert r.net_at_real_fees == 1.0 and r.filled is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-324 && uv run pytest research/nautilus_scalping/tests/test_maker_fill.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'maker_fill'`.

- [ ] **Step 3: Write the minimal module**

```python
# research/nautilus_scalping/maker_fill.py
"""ROB-324 — PURE maker/limit-fill scenario builders (no nautilus import).

Consumes ``MakerTradeRecord``s emitted by the maker re-sim and produces plain
``validated_gate.Trade`` lists for the unchanged gate. Fees are the REAL Binance
USDⓈ-M Futures Demo schedule (maker 2.0 / taker 4.0 bps) captured in
``results/rob324/binance_usdm_commission_rates.json``.

Gate convention (see spec §3.5): maker scenarios cannot use the gate's single-rate
fee rescale (mixed maker/taker legs), so each ``Trade`` carries the TRUE net at real
per-leg fees in ``net_ref_pnl`` and the true commission magnitude in
``commission_ref``. The driver evaluates maker scenarios at ``REF_FEE_BPS`` (scale=0
→ net_after_cost = as-run) and 0 (gross adds commission back). The taker baseline,
being single-rate, uses the gate's NATIVE rescale (call ``evaluate_gate`` at 4.0 bps
on the raw 10-bps taker trades) — no builder needed for it.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import blake2b

REF_FEE_BPS = 10.0          # mirrors validated_gate.REF_FEE_BPS (as-run reference point)
TAKER_BASELINE_BPS = 4.0    # real demo taker
MAKER_FEE_BPS = 2.0         # real demo maker


@dataclass(frozen=True)
class MakerTradeRecord:
    net_at_real_fees: float      # realized pnl already net of maker/taker per-leg fees
    commission_real: float       # total commission magnitude actually paid (>= 0)
    notional: float
    ts_opened: int
    filled: bool                 # False = limit cancelled (missed fill)
    tp_hit: bool                 # exit was the maker-limit TP (vs taker-stop SL)
    adverse_excursion_bps: float # worst adverse move between fill and exit, bps
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/mgh3326/work/auto_trader.rob-324 && uv run pytest research/nautilus_scalping/tests/test_maker_fill.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-324
git add research/nautilus_scalping/maker_fill.py research/nautilus_scalping/tests/test_maker_fill.py
git commit -m "feat(rob-324): maker_fill module skeleton + MakerTradeRecord

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 3: `build_maker_optimistic` (filled-only, true-net Trades)

**Files:**
- Modify: `research/nautilus_scalping/maker_fill.py`
- Test: `research/nautilus_scalping/tests/test_maker_fill.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_maker_fill.py`:

```python
from maker_fill import build_maker_optimistic
from validated_gate import Trade, metrics_at_fee


def test_optimistic_excludes_missed_fills_and_preserves_true_net() -> None:
    recs = [
        _rec(0.50, 0.04, 100.0, 1, filled=True),
        _rec(0.00, 0.00, 100.0, 2, filled=False),   # missed fill -> dropped
        _rec(-0.30, 0.04, 100.0, 3, filled=True),
    ]
    trades = build_maker_optimistic(recs)
    assert len(trades) == 2                          # missed fill excluded
    assert all(isinstance(t, Trade) for t in trades)
    # net_after_cost at the gate's reference point == as-run true net
    m = metrics_at_fee(trades, fee_bps=10.0, fold="net_after_cost")
    assert round(m.net_pnl, 2) == 0.20               # 0.50 + (-0.30)
    # gross (fee=0) adds the real commission back
    g = metrics_at_fee(trades, fee_bps=0.0, fold="gross")
    assert round(g.net_pnl, 2) == 0.28               # 0.20 + 0.04 + 0.04
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-324 && uv run pytest research/nautilus_scalping/tests/test_maker_fill.py::test_optimistic_excludes_missed_fills_and_preserves_true_net -v`
Expected: FAIL — `ImportError: cannot import name 'build_maker_optimistic'`.

- [ ] **Step 3: Implement**

Append to `maker_fill.py`:

```python
from validated_gate import Trade  # pure import (stdlib-only module)


def build_maker_optimistic(records: list[MakerTradeRecord]) -> list[Trade]:
    """Filled maker trades at real fees; missed fills contribute nothing.

    net_ref_pnl carries the true net (maker 2 / taker 4 bps already applied);
    commission_ref carries the true commission magnitude so the gate's gross
    column reconstructs correctly. Evaluate at REF_FEE_BPS for as-run net."""
    return [
        Trade(net_ref_pnl=r.net_at_real_fees, commission_ref=r.commission_real,
              notional=r.notional, ts_opened=r.ts_opened)
        for r in records if r.filled
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/mgh3326/work/auto_trader.rob-324 && uv run pytest research/nautilus_scalping/tests/test_maker_fill.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-324
git add research/nautilus_scalping/maker_fill.py research/nautilus_scalping/tests/test_maker_fill.py
git commit -m "feat(rob-324): build_maker_optimistic (filled-only, true-net Trades)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 4: conservative overlay — `classify_easy_tp` + `build_maker_conservative`

**Files:**
- Modify: `research/nautilus_scalping/maker_fill.py`
- Test: `research/nautilus_scalping/tests/test_maker_fill.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_maker_fill.py`:

```python
from maker_fill import build_maker_conservative, classify_easy_tp


def test_classify_easy_tp_boundary() -> None:
    # TP hit with tiny adverse excursion = easy (front-of-queue) fill
    assert classify_easy_tp(_rec(0.5, 0.04, 100, 1, tp_hit=True, adverse=1.0)) is True
    # TP hit but price moved against us first = a real fill, not easy
    assert classify_easy_tp(_rec(0.5, 0.04, 100, 1, tp_hit=True, adverse=5.0)) is False
    # SL exits are never "easy TP"
    assert classify_easy_tp(_rec(-0.5, 0.04, 100, 1, tp_hit=False, adverse=0.0)) is False


def test_conservative_applies_adverse_cost_to_survivors() -> None:
    # a non-easy filled trade is kept but charged the adverse cost
    recs = [_rec(0.50, 0.04, 100.0, 1, tp_hit=True, adverse=9.0)]  # not easy -> kept
    trades = build_maker_conservative(recs, queue_loss_pct=0.25, adverse_bps=1.0)
    assert len(trades) == 1
    # adverse cost = 1.0 bp * 100 notional / 10_000 = 0.01
    assert round(trades[0].net_ref_pnl, 4) == 0.49


def test_conservative_drops_about_quarter_of_easy_tp_fills_deterministically() -> None:
    easy = [_rec(0.50, 0.04, 100.0, ts, tp_hit=True, adverse=0.0) for ts in range(2000)]
    first = build_maker_conservative(easy, queue_loss_pct=0.25, adverse_bps=1.0)
    second = build_maker_conservative(easy, queue_loss_pct=0.25, adverse_bps=1.0)
    # deterministic across runs (hash of ts, not RNG)
    assert [t.ts_opened for t in first] == [t.ts_opened for t in second]
    kept = len(first)
    dropped = 2000 - kept
    assert 400 <= dropped <= 600          # ~25% dropped (hash uniformity tolerance)


def test_conservative_excludes_missed_fills() -> None:
    recs = [_rec(0.0, 0.0, 100.0, 1, filled=False)]
    assert build_maker_conservative(recs) == []


def test_conservative_is_strictly_worse_than_optimistic() -> None:
    recs = [_rec(0.50, 0.04, 100.0, ts, tp_hit=True, adverse=0.0) for ts in range(500)]
    from validated_gate import metrics_at_fee
    opt = metrics_at_fee(build_maker_optimistic(recs), 10.0).net_pnl
    con = metrics_at_fee(build_maker_conservative(recs), 10.0).net_pnl
    assert con < opt                      # queue-loss + adverse cost only ever hurt
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /Users/mgh3326/work/auto_trader.rob-324 && uv run pytest research/nautilus_scalping/tests/test_maker_fill.py -k conservative -v`
Expected: FAIL — `ImportError: cannot import name 'build_maker_conservative'`.

- [ ] **Step 3: Implement**

Append to `maker_fill.py`:

```python
def classify_easy_tp(record: MakerTradeRecord, excursion_eps_bps: float = 2.0) -> bool:
    """A TP fill that barely moved against us before reaching target — i.e. a
    front-of-queue fill we would not realistically win against real queue priority."""
    return record.tp_hit and record.adverse_excursion_bps <= excursion_eps_bps


def _uniform_from_ts(ts_opened: int) -> float:
    """Deterministic uniform [0,1) from the trade timestamp (reproducible, no RNG)."""
    digest = blake2b(str(ts_opened).encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2.0 ** 64


def build_maker_conservative(
    records: list[MakerTradeRecord],
    *,
    queue_loss_pct: float = 0.25,
    adverse_bps: float = 1.0,
    excursion_eps_bps: float = 2.0,
) -> list[Trade]:
    """Conservative maker scenario: an honest lower bound on the re-sim.

    Two haircuts on top of the data-derived fills:
      1. Queue loss — deterministically drop ``queue_loss_pct`` of the easy-TP fills
         (Nautilus has no order-queue model, so it over-fills passive limits).
      2. Adverse selection — charge ``adverse_bps`` on every surviving maker entry.
    Missed fills are excluded (they earn nothing)."""
    out: list[Trade] = []
    for r in records:
        if not r.filled:
            continue
        if classify_easy_tp(r, excursion_eps_bps) and _uniform_from_ts(r.ts_opened) < queue_loss_pct:
            continue  # queue loss
        adverse_cost = adverse_bps * r.notional / 10_000.0
        out.append(Trade(
            net_ref_pnl=r.net_at_real_fees - adverse_cost,
            commission_ref=r.commission_real,
            notional=r.notional, ts_opened=r.ts_opened,
        ))
    return out
```

- [ ] **Step 4: Run tests to verify pass**

Run: `cd /Users/mgh3326/work/auto_trader.rob-324 && uv run pytest research/nautilus_scalping/tests/test_maker_fill.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-324
git add research/nautilus_scalping/maker_fill.py research/nautilus_scalping/tests/test_maker_fill.py
git commit -m "feat(rob-324): conservative overlay (queue-loss drop + adverse cost)

Covers the adverse-selection and missed-fill paths (issue acceptance criterion).

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 5: Nautilus limit-fill mechanics spike (DE-RISK — verify before building the strategy)

The maker scenario assumes Nautilus's backtest matching engine (a) fills a resting LIMIT entry against trade ticks and charges **maker** fee, and (b) lets us cancel an unfilled limit. This task validates those assumptions on a 1-day slice **before** the full strategy is built. No production code; throwaway script printed to stdout.

**Files:** none committed (exploratory; delete after).

- [ ] **Step 1: Write a throwaway probe**

```bash
cat > /tmp/rob324_limit_probe.py <<'PY'
from pathlib import Path
from decimal import Decimal
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, TimeInForce
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money, Price
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.trading.strategy import Strategy, StrategyConfig
import os

CATALOG = os.environ["CATALOG"]


class ProbeCfg(StrategyConfig, frozen=True):
    instrument_id: object


class Probe(Strategy):
    def __init__(self, cfg):
        super().__init__(cfg)
        self._submitted = False
        self._order = None

    def on_start(self):
        self._inst = self.cache.instrument(self.config.instrument_id)
        self.subscribe_trade_ticks(self.config.instrument_id)

    def on_trade_tick(self, tick):
        if not self._submitted:
            # post a passive BUY limit 20 bps BELOW the first tick -> should rest, then
            # fill maker when price dips to it (or never -> we cancel in on_stop)
            px = tick.price.as_decimal() * Decimal("0.998")
            self._order = self.order_factory.limit(
                instrument_id=self.config.instrument_id, order_side=OrderSide.BUY,
                quantity=self._inst.make_qty(Decimal("100")),
                price=self._inst.make_price(px), time_in_force=TimeInForce.GTC)
            self.submit_order(self._order)
            self._submitted = True

    def on_order_filled(self, event):
        self.log.error(f"FILLED liquidity={event.liquidity_side} comm={event.commission}")

    def on_stop(self):
        for o in self.cache.orders_open(instrument_id=self.config.instrument_id):
            self.cancel_order(o)


cat_obj = ParquetDataCatalog(CATALOG)
inst = next(i for i in cat_obj.instruments() if i.id.value.startswith("XRPUSDT"))
# rebuild instrument at REAL demo fees: maker 2bps / taker 4bps
from nautilus_trader.model.instruments import CurrencyPair
inst2 = CurrencyPair(
    instrument_id=inst.id, raw_symbol=inst.raw_symbol, base_currency=inst.base_currency,
    quote_currency=inst.quote_currency, price_precision=inst.price_precision,
    size_precision=inst.size_precision, price_increment=inst.price_increment,
    size_increment=inst.size_increment, lot_size=inst.lot_size,
    max_quantity=inst.max_quantity, min_quantity=inst.min_quantity,
    max_notional=inst.max_notional, min_notional=inst.min_notional,
    max_price=inst.max_price, min_price=inst.min_price,
    margin_init=inst.margin_init, margin_maint=inst.margin_maint,
    maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0004"),
    ts_event=0, ts_init=0)
ticks = cat_obj.trade_ticks(instrument_ids=[inst.id.value])[:200_000]
eng = BacktestEngine(config=BacktestEngineConfig(trader_id="PROBE-001",
      logging=LoggingConfig(log_level="ERROR")))
eng.add_venue(venue=Venue("BINANCE"), oms_type=OmsType.HEDGING,
              account_type=AccountType.CASH, base_currency=None,
              starting_balances=[Money(10_000_000, USDT)])
eng.add_instrument(inst2)
eng.add_data(ticks)
eng.add_strategy(Probe(ProbeCfg(instrument_id=inst2.id)))
eng.run()
print("positions_closed:", len(eng.cache.positions_closed()))
for p in eng.cache.positions_closed()[:3]:
    print("  comm:", [str(c) for c in p.commissions()], "pnl:", p.realized_pnl)
eng.dispose()
PY
```

- [ ] **Step 2: Run the probe and record observations**

Run:
```bash
cd /Users/mgh3326/work/auto_trader.rob-324/research/nautilus_scalping
PYTHONPATH=../.. $NVENV /tmp/rob324_limit_probe.py
```
Expected observations to record in the task notes:
- At least one `FILLED liquidity=...` line. **Confirm `liquidity_side` is `MAKER`** for the resting limit. If it shows `TAKER`, the entry limit is being treated as marketable — note it; the strategy must post the entry passively enough to rest (the design posts at the signal close on a fade entry, which is already a local low, so it should rest).
- The maker commission on a filled entry ≈ `notional * 0.0002` (2 bps). Sanity-check the printed `comm`.
- `cancel_order` in `on_stop` raises no error (unfilled orders cancellable).

- [ ] **Step 3: Decide & document**

If maker fills + cancel both work → proceed to Task 6 as written. If the entry limit fills as TAKER even when posted below market, document the finding and adjust Task 6 to post the entry limit one tick more passive (still data-derived) — do **not** silently keep taker fees on the "maker" scenario. Delete `/tmp/rob324_limit_probe.py`. No commit.

---

## Task 6: `strategy_meanrev.py` — `execution_mode="maker"` branch

**Files:**
- Modify: `research/nautilus_scalping/strategy_meanrev.py`
- Test (regression): `research/nautilus_scalping/tests/test_meanrev_parity.py` (must stay green; default is taker)

- [ ] **Step 1: Add the config flag + maker state**

In `MeanRevScalperConfig`, add (after `allow_short`):

```python
    execution_mode: str = "taker"   # "taker" (default, unchanged) | "maker"
    fill_timeout_bars: int = 1      # cancel an unfilled maker entry after N closed bars
```

In `MeanRevScalper.__init__`, after the existing attributes, add:

```python
        # maker-mode bookkeeping
        self._entry_order = None
        self._entry_submitted_bar: int | None = None
        self._bar_count = 0
        self._tp_order = None
        self._entry_px: Decimal | None = None
        self._adverse_px: Decimal | None = None   # worst price seen vs entry while in position
        self.records: list[dict] = []             # maker: completed trade records
        self.entries_attempted = 0
        self.entries_filled = 0
```

- [ ] **Step 2: Branch `on_bar` / `_enter` by execution mode**

Replace `on_bar` and `_enter` with mode-aware versions (taker path is byte-identical to before):

```python
    def on_bar(self, bar: Bar) -> None:
        self._bar_count += 1
        self._candles.append(bar_to_candle(bar))

        # maker: cancel a stale unfilled entry limit (missed fill)
        if (self.config.execution_mode == "maker" and self._entry_order is not None
                and self._entry_submitted_bar is not None
                and self._bar_count - self._entry_submitted_bar >= self.config.fill_timeout_bars):
            self.cancel_order(self._entry_order)
            self._entry_order = None
            self._entry_submitted_bar = None

        if len(self._candles) < self._needed:
            return
        if not self.portfolio.is_flat(self.config.instrument_id):
            return
        if self._entry_order is not None:   # maker: a limit is still working
            return
        d = evaluate_meanrev(list(self._candles), self._cfg)
        if d.has_entry and d.side == "BUY":
            self._enter(OrderSide.BUY, d.entry_price, d.tp_price, d.sl_price)
        elif d.has_entry and d.side == "SELL":
            self._enter(OrderSide.SELL, d.entry_price, d.tp_price, d.sl_price)

    def _enter(self, side, entry, tp, sl) -> None:
        self._tp, self._sl, self._side = tp, sl, side
        if self.config.execution_mode == "taker":
            order = self.order_factory.market(
                instrument_id=self.config.instrument_id, order_side=side,
                quantity=self._instrument.make_qty(Decimal(self.config.trade_size)))
            self.submit_order(order)
            return
        # maker: passive limit entry at the signal close (fade entry rests at a local low)
        self.entries_attempted += 1
        order = self.order_factory.limit(
            instrument_id=self.config.instrument_id, order_side=side,
            quantity=self._instrument.make_qty(Decimal(self.config.trade_size)),
            price=self._instrument.make_price(entry))
        self._entry_order = order
        self._entry_submitted_bar = self._bar_count
        self.submit_order(order)
```

- [ ] **Step 3: Add maker fill/exit handling + record building**

Add these methods (taker mode never calls them because it never submits limit entries / TP limits):

```python
    def on_order_filled(self, event) -> None:
        if self.config.execution_mode != "maker":
            return
        if self._entry_order is not None and event.client_order_id == self._entry_order.client_order_id:
            # entry filled -> post a resting maker-limit TP; SL handled on ticks
            self.entries_filled += 1
            self._entry_order = None
            self._entry_submitted_bar = None
            self._entry_px = event.last_px.as_decimal()
            self._adverse_px = self._entry_px
            self._tp_order = self.order_factory.limit(
                instrument_id=self.config.instrument_id,
                order_side=(OrderSide.SELL if self._side == OrderSide.BUY else OrderSide.BUY),
                quantity=event.last_qty,
                price=self._instrument.make_price(self._tp))
            self.submit_order(self._tp_order)

    def on_trade_tick(self, tick: TradeTick) -> None:
        if self.config.execution_mode == "taker":
            return self._taker_exit_check(tick)
        # maker: track adverse excursion; trigger taker SL via market if breached
        if self.portfolio.is_flat(self.config.instrument_id) or self._entry_px is None:
            return
        price = tick.price.as_decimal()
        if self._side == OrderSide.BUY:
            self._adverse_px = min(self._adverse_px, price)
            if self._sl is not None and price <= self._sl:
                self._maker_sl_exit()
        else:
            self._adverse_px = max(self._adverse_px, price)
            if self._sl is not None and price >= self._sl:
                self._maker_sl_exit()

    def _taker_exit_check(self, tick: TradeTick) -> None:
        # unchanged taker logic
        if self.portfolio.is_flat(self.config.instrument_id):
            return
        price = tick.price.as_decimal()
        if self._side == OrderSide.BUY:
            if self._sl is not None and price <= self._sl:
                self._exit()
            elif self._tp is not None and price >= self._tp:
                self._exit()
        else:
            if self._sl is not None and price >= self._sl:
                self._exit()
            elif self._tp is not None and price <= self._tp:
                self._exit()

    def _maker_sl_exit(self) -> None:
        if self._tp_order is not None:
            self.cancel_order(self._tp_order)
            self._tp_order = None
        self.close_all_positions(self.config.instrument_id)  # taker stop-out

    def on_position_closed(self, event) -> None:
        if self.config.execution_mode != "maker":
            return
        pos = self.cache.position(event.position_id)
        entry = self._entry_px if self._entry_px is not None else Decimal(str(pos.avg_px_open))
        if self._side == OrderSide.BUY:
            adverse = (entry - (self._adverse_px or entry)) / entry * Decimal("10000")
        else:
            adverse = ((self._adverse_px or entry) - entry) / entry * Decimal("10000")
        tp_hit = self._tp_order is not None  # TP order existed and was not cancelled by SL
        self.records.append({
            "net": pos.realized_pnl.as_double(),
            "comm": sum(c.as_double() for c in pos.commissions()),
            "notional": float(pos.avg_px_open) * float(pos.peak_qty),
            "ts": int(pos.ts_opened),
            "filled": True,
            "tp_hit": bool(tp_hit),
            "adverse_bps": float(max(Decimal("0"), adverse)),
        })
        self._tp_order = None
        self._entry_px = None
        self._adverse_px = None
        self._tp = self._sl = self._side = None
```

> **Note on `tp_hit`:** when the TP limit fills, the position closes with the TP order still referenced (not cancelled) → `tp_hit=True`. When SL fires, `_maker_sl_exit` cancels the TP order first → `tp_hit=False`. This is the deterministic signal the conservative overlay's `classify_easy_tp` uses.

- [ ] **Step 4: Keep the existing `_exit` (taker) and run the parity regression**

`_exit` and `on_stop` stay as-is. Verify the taker default is unchanged:

Run: `cd /Users/mgh3326/work/auto_trader.rob-324/research/nautilus_scalping && PYTHONPATH=../.. $NVENV -m pytest tests/test_meanrev_parity.py tests/test_meanrev_signal.py -v`
Expected: PASS (taker behavior untouched; default `execution_mode="taker"`).

- [ ] **Step 5: Smoke the maker strategy end-to-end on one symbol**

```bash
cd /Users/mgh3326/work/auto_trader.rob-324/research/nautilus_scalping
PYTHONPATH=../.. $NVENV -c "
from pathlib import Path
from decimal import Decimal
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.instruments import CurrencyPair
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from strategy_meanrev import MeanRevScalper, MeanRevScalperConfig
import os
c = ParquetDataCatalog(os.environ['CATALOG'])
inst = next(i for i in c.instruments() if i.id.value.startswith('XRPUSDT'))
inst = CurrencyPair(instrument_id=inst.id, raw_symbol=inst.raw_symbol, base_currency=inst.base_currency, quote_currency=inst.quote_currency, price_precision=inst.price_precision, size_precision=inst.size_precision, price_increment=inst.price_increment, size_increment=inst.size_increment, lot_size=inst.lot_size, max_quantity=inst.max_quantity, min_quantity=inst.min_quantity, max_notional=inst.max_notional, min_notional=inst.min_notional, max_price=inst.max_price, min_price=inst.min_price, margin_init=inst.margin_init, margin_maint=inst.margin_maint, maker_fee=Decimal('0.0002'), taker_fee=Decimal('0.0004'), ts_event=0, ts_init=0)
e = BacktestEngine(config=BacktestEngineConfig(trader_id='SMOKE-001', logging=LoggingConfig(log_level='ERROR')))
e.add_venue(venue=Venue('BINANCE'), oms_type=OmsType.HEDGING, account_type=AccountType.CASH, base_currency=None, starting_balances=[Money(10_000_000, USDT)])
e.add_instrument(inst); e.add_data(c.trade_ticks(instrument_ids=[inst.id.value]))
s = MeanRevScalper(MeanRevScalperConfig(instrument_id=inst.id, bar_type=BarType.from_str(f'{inst.id.value}-1-MINUTE-LAST-INTERNAL'), trade_size='100', execution_mode='maker'))
e.add_strategy(s); e.run()
print('attempted', s.entries_attempted, 'filled', s.entries_filled, 'records', len(s.records))
assert s.entries_attempted > 0 and len(s.records) > 0
miss = s.entries_attempted - s.entries_filled
print('missed fills', miss)
print('sample', s.records[0])
e.dispose()
"
```
Expected: prints non-zero attempted/filled/records, a plausible missed-fill count, and a sample record with `tp_hit`/`adverse_bps`. (No assertion on the exact numbers — this only proves the wiring runs.)

- [ ] **Step 6: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-324
git add research/nautilus_scalping/strategy_meanrev.py
git commit -m "feat(rob-324): maker execution_mode for MeanRevScalper (limit entry, maker TP, taker SL)

Default execution_mode=taker preserves ROB-320 behavior. Maker mode posts a
passive limit entry (1-bar timeout = missed fill), a resting maker-limit TP, and
a taker stop-out SL; records per-trade net/commission/adverse-excursion/tp_hit.

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 7: `backtest_runner.py` — maker-mode run (real fees + records + counters)

**Files:**
- Modify: `research/nautilus_scalping/backtest_runner.py`

- [ ] **Step 1: Add a real-fee instrument helper + maker branch in `_run_single`**

At the top of `_run_single`'s nautilus imports, also import `CurrencyPair`:

```python
    from nautilus_trader.model.instruments import CurrencyPair
```

After `instrument = next(...)` is resolved and **before** `engine.add_instrument(instrument)`, insert the maker fee override:

```python
    execution_mode = str(params.get("execution_mode", "taker"))
    if execution_mode == "maker":
        instrument = CurrencyPair(
            instrument_id=instrument.id, raw_symbol=instrument.raw_symbol,
            base_currency=instrument.base_currency, quote_currency=instrument.quote_currency,
            price_precision=instrument.price_precision, size_precision=instrument.size_precision,
            price_increment=instrument.price_increment, size_increment=instrument.size_increment,
            lot_size=instrument.lot_size, max_quantity=instrument.max_quantity,
            min_quantity=instrument.min_quantity, max_notional=instrument.max_notional,
            min_notional=instrument.min_notional, max_price=instrument.max_price,
            min_price=instrument.min_price, margin_init=instrument.margin_init,
            margin_maint=instrument.margin_maint,
            maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0004"),   # real demo 2/4 bps
            ts_event=0, ts_init=0)
```

Add `from decimal import Decimal` to the top-level imports of `backtest_runner.py` (module scope) if not present.

- [ ] **Step 2: Wire `execution_mode` into the meanrev strategy and emit maker records**

In the `meanrev_zscore_fade` branch, pass the flag:

```python
        strat = MeanRevScalper(MeanRevScalperConfig(
            instrument_id=instrument.id, bar_type=bar_type, trade_size=trade_size,
            lookback=int(params.get("lookback", 20)), z_entry=str(params.get("z_entry", "2.0")),
            tp_bps=int(params.get("tp_bps", 30)), sl_bps=int(params.get("sl_bps", 30)),
            require_vol=bool(params.get("require_vol", True)),
            execution_mode=execution_mode))
```

Replace the trade-emission tail of `_run_single` so maker runs emit the rich records + counters:

```python
    engine.add_strategy(strat)
    engine.run()
    if execution_mode == "maker":
        payload = {
            "execution_mode": "maker",
            "records": list(strat.records),
            "entries_attempted": int(strat.entries_attempted),
            "entries_filled": int(strat.entries_filled),
        }
        engine.dispose()
        print(_SENTINEL + json.dumps(payload))
        return
    trades = []
    for p in engine.cache.positions_closed():
        net_ref = p.realized_pnl.as_double()
        comm_ref = sum(c.as_double() for c in p.commissions())
        notional = float(p.avg_px_open) * float(p.peak_qty)
        trades.append([net_ref, comm_ref, notional, int(p.ts_opened)])
    engine.dispose()
    print(_SENTINEL + json.dumps({"trades": trades}))
```

- [ ] **Step 3: Add a maker-aware `run_maker(...)` helper that returns `MakerTradeRecord`s**

Add after the existing `run(...)`:

```python
def run_maker(catalog: Path, symbol: str, params: dict, trade_size: str = "100"):
    """Run the maker re-sim; return (records, attempted, filled).

    records are maker_fill.MakerTradeRecord; attempted-filled = missed fills."""
    from maker_fill import MakerTradeRecord
    p = dict(params); p["execution_mode"] = "maker"
    venv_python = sys.executable
    cmd = [venv_python, os.path.abspath(__file__), "--single", "--catalog", str(catalog),
           "--symbol", symbol, "--strategy", "meanrev_zscore_fade",
           "--params", json.dumps(p), "--trade-size", trade_size]
    env = dict(os.environ)
    env["PYTHONPATH"] = env.get("PYTHONPATH", "") + os.pathsep + "../.."
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    for line in proc.stdout.splitlines():
        if line.startswith(_SENTINEL):
            data = json.loads(line[len(_SENTINEL):])
            recs = [MakerTradeRecord(
                net_at_real_fees=r["net"], commission_real=abs(r["comm"]),
                notional=r["notional"], ts_opened=r["ts"], filled=r["filled"],
                tp_hit=r["tp_hit"], adverse_excursion_bps=r["adverse_bps"])
                for r in data["records"]]
            return recs, data["entries_attempted"], data["entries_filled"]
    raise RuntimeError(f"maker runner {symbol} failed:\n{proc.stderr[-800:]}")
```

- [ ] **Step 4: Smoke `run_maker` for both symbols**

```bash
cd /Users/mgh3326/work/auto_trader.rob-324/research/nautilus_scalping
PYTHONPATH=../.. $NVENV -c "
from pathlib import Path; import os
from backtest_runner import run_maker
for sym, size in [('XRPUSDT','100'), ('BTCUSDT','0.002')]:
    recs, att, fil = run_maker(Path(os.environ['CATALOG']), sym, {'lookback':20,'z_entry':'2.0','tp_bps':30,'sl_bps':30}, size)
    print(sym, 'records', len(recs), 'attempted', att, 'filled', fil, 'missed', att-fil)
    assert len(recs) > 0
print('OK')
"
```
Expected: both symbols print record/attempt/fill/miss counts and `OK`.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-324
git add research/nautilus_scalping/backtest_runner.py
git commit -m "feat(rob-324): backtest_runner maker mode (real 2/4bps fees, records, fill counts)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 8: `validate_maker_fill.py` — driver + v2 artifact

**Files:**
- Create: `research/nautilus_scalping/validate_maker_fill.py`

- [ ] **Step 1: Write the driver**

```python
#!/usr/bin/env python3
"""ROB-324 — maker/limit-fill edge re-evaluation driver.

Produces three scenarios and feeds each to the UNCHANGED validated_gate:
  1. taker_baseline   — ROB-320 taker trades, gate's native rescale to 4.0 bps
  2. maker_optimistic — data-derived limit fills at real maker/taker fees
  3. maker_conservative — (2) minus queue-loss drop + adverse-selection cost  [HEADLINE]

NO execution side effects: public-data backtest only. Nothing submits, schedules,
mutates a broker/DB, reads secrets, or applies params to a daemon.

Usage (rob-320 venv):
    export CATALOG=/Users/mgh3326/work/auto_trader.rob-320/research/nautilus_scalping/catalog
    PYTHONPATH=../.. $NVENV validate_maker_fill.py --catalog "$CATALOG" \
        --symbols XRPUSDT,BTCUSDT --window-from 2026-03-01 --window-to 2026-05-14 \
        --export results/rob324/maker_fill.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from backtest_runner import run, run_maker
from candidates import get_candidate
from maker_fill import (
    MAKER_FEE_BPS,
    TAKER_BASELINE_BPS,
    build_maker_conservative,
    build_maker_optimistic,
)
from validated_gate import REF_FEE_BPS, Trade, evaluate_gate

_GRID = [
    ("z2.0/tp30/sl30", {"lookback": 20, "z_entry": "2.0", "tp_bps": 30, "sl_bps": 30}),
    ("z2.5/tp40/sl40", {"lookback": 20, "z_entry": "2.5", "tp_bps": 40, "sl_bps": 40}),
]
_SIZE = {"BTCUSDT": "0.002"}
_FILL_MODEL = {
    "entry_rule": "passive limit @ signal-bar close",
    "fill_timeout_bars": 1,
    "tp_execution": "maker limit",
    "sl_execution": "taker stop (market)",
    "queue_loss_pct": 0.25,
    "adverse_bps": 1.0,
    "excursion_eps_bps": 2.0,
}


def _merge(runs: list[list[Trade]]) -> list[Trade]:
    return sorted((t for r in runs for t in r), key=lambda t: t.ts_opened)


def _merge_recs(runs):
    return sorted((rec for r in runs for rec in r), key=lambda rec: rec.ts_opened)


def _gate(candidate_runs, breakout, random_ctrl, fee_bps, symbols, window, name):
    return evaluate_gate(
        candidate_runs=candidate_runs,
        baseline_breakout=breakout, baseline_random=random_ctrl,
        fee_bps=fee_bps, min_trades=100,
        candidate_name=name, hypothesis="mean_reversion",
        symbols=symbols, window=window,
    ).to_dict()


def main() -> int:
    ap = argparse.ArgumentParser(description="ROB-324 maker/limit-fill driver")
    ap.add_argument("--catalog", type=Path, default="catalog")
    ap.add_argument("--symbols", default="XRPUSDT,BTCUSDT")
    ap.add_argument("--window-from", default="")
    ap.add_argument("--window-to", default="")
    ap.add_argument("--export", type=Path, default="results/rob324/maker_fill.json")
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    window = {"from": args.window_from, "to": args.window_to,
              "folds": {"train": 0.5, "val": 0.25, "oos": 0.25}}

    # --- baselines (taker, single config each), merged across symbols ---
    print("Running taker baselines (breakout, random)...")
    breakout = _merge([run(args.catalog, s, "micro_breakout",
                           get_candidate("micro_breakout").default_params,
                           _SIZE.get(s, "100")) for s in symbols])
    random_ctrl = _merge([run(args.catalog, s, "random_entry",
                              get_candidate("random_entry").default_params,
                              _SIZE.get(s, "100")) for s in symbols])

    # --- scenario 1: taker baseline candidate over the param grid (gate rescales 10->4) ---
    print("Scenario 1: taker baseline @ 4 bps (grid)...")
    taker_runs = {label: _merge([run(args.catalog, s, "meanrev_zscore_fade",
                                     dict(params), _SIZE.get(s, "100")) for s in symbols])
                  for label, params in _GRID}
    taker_report = _gate(taker_runs, breakout, random_ctrl, TAKER_BASELINE_BPS,
                         symbols, window, "meanrev_taker_baseline")

    # --- scenarios 2 & 3: maker re-sim over the SAME grid (param-stability preserved) ---
    print("Scenarios 2 & 3: maker re-sim (grid)...")
    maker_recs: dict[str, list] = {}
    attempted = filled = 0
    for label, params in _GRID:
        per_symbol = []
        for s in symbols:
            recs, att, fil = run_maker(args.catalog, s, dict(params), _SIZE.get(s, "100"))
            per_symbol.append(recs)
            attempted += att
            filled += fil
        maker_recs[label] = _merge_recs(per_symbol)

    opt_runs = {label: build_maker_optimistic(recs) for label, recs in maker_recs.items()}
    con_runs = {label: build_maker_conservative(
                    recs, queue_loss_pct=_FILL_MODEL["queue_loss_pct"],
                    adverse_bps=_FILL_MODEL["adverse_bps"],
                    excursion_eps_bps=_FILL_MODEL["excursion_eps_bps"])
                for label, recs in maker_recs.items()}

    # maker scenarios: net already at real fees -> evaluate at REF (as-run)
    opt_report = _gate(opt_runs, breakout, random_ctrl, REF_FEE_BPS,
                       symbols, window, "meanrev_maker_optimistic")
    con_report = _gate(con_runs, breakout, random_ctrl, REF_FEE_BPS,
                       symbols, window, "meanrev_maker_conservative")

    artifact = {
        "schema_version": "validated_signal_gate.v2",
        "candidate": "meanrev_zscore_fade",
        "hypothesis": "mean_reversion",
        "symbols": symbols,
        "window": window,
        "cost_model": {
            "maker_fee_bps": MAKER_FEE_BPS, "taker_fee_bps": TAKER_BASELINE_BPS,
            "commission_source": "results/rob324/binance_usdm_commission_rates.json",
            "note": ("maker scenarios bake real per-leg fees into net; gate evaluated "
                     "at its reference point (as-run). taker baseline uses the gate's "
                     "native single-rate rescale to 4.0 bps."),
        },
        "fill_model": _FILL_MODEL,
        "fill_stats": {"entries_attempted": attempted, "entries_filled": filled,
                       "missed_fills": attempted - filled},
        "scenarios": {
            "taker_baseline": taker_report,
            "maker_optimistic": opt_report,
            "maker_conservative": con_report,
        },
        "verdict": con_report["verdict"],            # headline = conservative (honest bound)
        "verdict_reasons": con_report["verdict_reasons"],
        "verdict_source": "maker_conservative",
    }

    args.export.parent.mkdir(parents=True, exist_ok=True)
    args.export.write_text(json.dumps(artifact, indent=2))
    print("\n=============================================================")
    print(f"HEADLINE VERDICT (conservative): {artifact['verdict'].upper()}")
    for r in artifact["verdict_reasons"]:
        print(f"  - {r}")
    print(f"taker_baseline   verdict: {taker_report['verdict']}")
    print(f"maker_optimistic verdict: {opt_report['verdict']}")
    print(f"missed fills: {attempted - filled} / {attempted} attempted")
    print(f"Report exported to: {args.export.resolve()}")
    print("=============================================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Syntax/lint check (no run yet)**

Run: `cd /Users/mgh3326/work/auto_trader.rob-324 && uv run ruff check research/nautilus_scalping/validate_maker_fill.py research/nautilus_scalping/maker_fill.py`
Expected: PASS (no errors).

- [ ] **Step 3: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-324
git add research/nautilus_scalping/validate_maker_fill.py
git commit -m "feat(rob-324): validate_maker_fill driver writes validated_signal_gate.v2 artifact

Three scenarios (taker 4bps, maker optimistic, maker conservative) through the
unchanged gate; headline verdict = conservative (honest lower bound).

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 9: Run the pipeline → produce `maker_fill.json`

**Files:**
- Create (generated): `research/nautilus_scalping/results/rob324/maker_fill.json`

- [ ] **Step 1: Run the full driver**

```bash
cd /Users/mgh3326/work/auto_trader.rob-324/research/nautilus_scalping
export NVENV=/Users/mgh3326/work/auto_trader.rob-320/research/nautilus_scalping/.venv/bin/python
export CATALOG=/Users/mgh3326/work/auto_trader.rob-320/research/nautilus_scalping/catalog
PYTHONPATH=../.. $NVENV validate_maker_fill.py --catalog "$CATALOG" \
    --symbols XRPUSDT,BTCUSDT --window-from 2026-03-01 --window-to 2026-05-14 \
    --export results/rob324/maker_fill.json
```
Expected: prints the headline conservative verdict plus the taker/optimistic verdicts and missed-fill count; writes `results/rob324/maker_fill.json`.

- [ ] **Step 2: Sanity-check the artifact**

```bash
cd /Users/mgh3326/work/auto_trader.rob-324
uv run python -c "
import json
a = json.load(open('research/nautilus_scalping/results/rob324/maker_fill.json'))
assert a['schema_version'] == 'validated_signal_gate.v2'
assert a['verdict'] in {'validated','not_validated','insufficient_data'}
assert set(a['scenarios']) == {'taker_baseline','maker_optimistic','maker_conservative'}
assert a['cost_model']['maker_fee_bps'] == 2.0 and a['cost_model']['taker_fee_bps'] == 4.0
for name, rep in a['scenarios'].items():
    oos = next(f for f in rep['per_fold'] if f['fold']=='oos')
    print(f\"{name:18} verdict={rep['verdict']:16} oos_net={oos['net_pnl']:.2f} oos_pf={oos['profit_factor']:.2f}\")
print('headline', a['verdict'], '| missed', a['fill_stats']['missed_fills'])
"
```
Expected: all asserts pass; prints each scenario's OOS net/PF and the headline verdict. (Per the analytic prior, expect the conservative — and likely optimistic — verdicts to be `not_validated`. That is a legitimate result, not a failure.)

- [ ] **Step 3: Commit the artifact (force-add past gitignore)**

```bash
cd /Users/mgh3326/work/auto_trader.rob-324
git add -f research/nautilus_scalping/results/rob324/maker_fill.json
git commit -m "feat(rob-324): maker/limit-fill re-evaluation result artifact

validated_signal_gate.v2 with taker(4bps)/maker-optimistic/maker-conservative
scenarios for XRPUSDT+BTCUSDT. Headline verdict from the conservative scenario.

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 10: Verdict-vocabulary gate test + final lint

**Files:**
- Modify: `research/nautilus_scalping/tests/test_maker_fill.py`

- [ ] **Step 1: Write a report-shape / verdict-vocabulary test (pure, synthetic)**

Append to `tests/test_maker_fill.py`:

```python
from validated_gate import evaluate_gate


def test_maker_scenario_verdict_stays_in_vocabulary() -> None:
    # synthetic filled maker records -> optimistic/conservative Trades -> gate
    recs = [_rec(0.20, 0.04, 100.0, ts, tp_hit=(ts % 2 == 0), adverse=float(ts % 5))
            for ts in range(400)]
    losers = [Trade(net_ref_pnl=-0.5, commission_ref=0.04, notional=100.0, ts_opened=ts)
              for ts in range(400)]
    for builder in (build_maker_optimistic, build_maker_conservative):
        trades = builder(recs)
        report = evaluate_gate(
            candidate_runs={"maker_fill": trades},
            baseline_breakout=losers, baseline_random=losers,
            fee_bps=10.0, min_trades=100,
            candidate_name="t", hypothesis="mean_reversion", symbols=["XRPUSDT"])
        assert report.verdict in {"validated", "not_validated", "insufficient_data"}
```

- [ ] **Step 2: Run the full pure suite**

Run: `cd /Users/mgh3326/work/auto_trader.rob-324 && uv run pytest research/nautilus_scalping/tests/test_maker_fill.py -v`
Expected: PASS (all).

- [ ] **Step 3: Lint everything touched**

Run: `cd /Users/mgh3326/work/auto_trader.rob-324 && uv run ruff check research/nautilus_scalping/`
Expected: PASS. Fix any findings, re-run.

- [ ] **Step 4: Run the nautilus-dependent regression once more**

Run: `cd /Users/mgh3326/work/auto_trader.rob-324/research/nautilus_scalping && PYTHONPATH=../.. $NVENV -m pytest tests/ -v`
Expected: PASS or `skipped` (nautilus-gated tests run here; pure tests pass).

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-324
git add research/nautilus_scalping/tests/test_maker_fill.py
git commit -m "test(rob-324): assert maker scenarios keep the three-value verdict vocabulary

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 11: PR handoff

- [ ] **Step 1: Push the branch and open the PR (base `main`)**

```bash
cd /Users/mgh3326/work/auto_trader.rob-324
git push -u origin rob-324
gh pr create --base main --title "feat(rob-324): maker/limit-fill scalping edge re-evaluation (research-only)" --body "$(cat <<'EOF'
## Summary
Research-only follow-up to ROB-320 (#968). Re-evaluates whether a conservative
maker/limit-fill execution model recovers a net-after-cost edge for
`meanrev_zscore_fade` on XRPUSDT + BTCUSDT, using the REAL Binance USDⓈ-M Futures
Demo fees (maker 2.0 / taker 4.0 bps) captured in the committed commission artifact.

## Artifact
`research/nautilus_scalping/results/rob324/maker_fill.json` (`validated_signal_gate.v2`):
three scenarios (taker 4 bps baseline, data-derived maker, conservative maker overlay).

## Verdict
Headline verdict (conservative scenario): **<fill from Task 9 output>**.
<one line on optimistic + taker baseline verdicts and missed-fill count>.

## Side-effect boundary
Research/backtest only. No live trading, no Demo `confirm=true`, no
broker/order/watch/order-intent mutation, no scheduler/Prefect/launchd, no prod
DB/env/secret changes, no runtime parameter application, no `/invest` surfacing.
Reads the rob-320 ParquetDataCatalog read-only. The captured commission artifact
contains no secrets, signatures, balances, positions, or account identifiers.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Fill the verdict line in the PR body from the Task 9 output, and post the issue handoff comment** (artifact path, verdict, side-effect boundary) on ROB-324.

---

## Self-review notes (author)

- **Spec coverage:** taker/maker/conservative scenarios (spec §2 → Tasks 8–9); real tick re-sim (§3.1, §3.2 → Tasks 5–7); pure overlay + missed-fill/adverse tests (§5 → Tasks 3–4, 10); gate unchanged + convention (§3.5 → Task 3 docstring, Task 8 `_gate`/`REF_FEE_BPS`); v2 artifact fields (§4 → Task 8); fee provenance (§4 → Task 1); execution prereqs (§6 → Conventions + Task 1 Step 2); safety boundary (§8 → driver docstring + PR body).
- **Type consistency:** `MakerTradeRecord` fields (`net_at_real_fees`, `commission_real`, `notional`, `ts_opened`, `filled`, `tp_hit`, `adverse_excursion_bps`) are identical across Tasks 2/3/4/7/10. `run_maker` returns `(records, attempted, filled)` and is consumed that way in Task 8. `build_maker_optimistic`/`build_maker_conservative` signatures match call sites. Strategy `records` dict keys (`net/comm/notional/ts/filled/tp_hit/adverse_bps`) map 1:1 to `run_maker`'s `MakerTradeRecord` construction.
- **Placeholder scan:** the only intentional fill-in is the PR-body verdict line (Task 11), which depends on the Task 9 run output and cannot be known in advance.
- **Risk:** Task 5 spike gates the maker-fee assumption before the strategy is built; if Nautilus treats the entry limit as taker, Task 6 is adjusted (documented, no silent taker-as-maker).

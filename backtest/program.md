# autotrader backtest — autoresearch program

Autonomous trading strategy research on Upbit spot crypto (daily bars).

## Context

This project uses the autoresearch pattern for trading strategy discovery.
The strategy has already been tuned through 204 experiments (baseline 0.84 → 4.23 cv_score).

Your job: **push cv_score higher** by modifying `strategy.py`.

- **Market:** Upbit KRW spot (no futures, no leverage)
- **Timeframe:** Daily bars (1시간봉 is future work)
- **Universe:** BTC, ETH, SOL, XRP, LINK, ADA, DOT, AVAX
- **Capital:** 10,000,000 KRW
- **Fees:** 0.05% commission + 2bps slippage
- **Evaluation:** Walk-forward CV (4 folds), NOT single split

## Current Leaderboard

Check `results.tsv` for full history. Top scores:

```
RANK  EXPERIMENT   CV_SCORE   MEAN      STD       MIN_FOLD
1.    exp204       4.228733   4.619197  0.780929  3.878618  ← CURRENT BEST
2.    exp203       4.228659   4.619131  0.780944  3.878618
3.    exp202       4.228511   4.619000  0.780976  3.878618
```

**Your goal: beat cv_score 4.228733.**

To get live numbers: `tail -20 results.tsv | sort -t$'\t' -k2 -rn | head -5`

## Rules

**What you CAN do:**
- Modify `backtest/strategy.py` — this is the ONLY file you edit.

**What you CANNOT do:**
- Modify `prepare.py`, `backtest.py`, `fetch_data.py`, or anything in `benchmarks/`.
- Install new packages. Only `numpy`, `pandas`, `ta`, `scipy`, and standard library.
- Look at or optimize for test set data.
- Make changes that take >120 seconds per CV run.

## Setup

1. Read `backtest/strategy.py`, `backtest/prepare.py`, `backtest/backtest.py`, this file.
2. Check current best: `tail -5 results.tsv`
3. Verify data exists: `ls backtest/data/`
4. Run baseline: `uv run backtest/backtest.py --mode cv` to confirm current score.

## Orchestrator Usage

For autonomous multi-round experiments:

```bash
# Manual mode: you or another AI agent commits strategy.py changes,
# orchestrator waits for the next commit and then runs one experiment round.
uv run backtest/orchestrator.py --mode manual --rounds 20

# Auto mode: orchestrator invokes the AI CLI, waits for a fresh commit,
# then runs one experiment round automatically.
uv run backtest/orchestrator.py --mode auto --rounds 50 --ai-cli claude
```

### Description Source

- `--description "<text>"` overrides every round
- If `--description` is omitted, orchestrator uses `git log -1 --format=%s`
- If there is no commit subject available, orchestrator stops with an error

### Manual Mode Behavior

- Orchestrator stores the current `HEAD` when it starts
- Each round waits for a new commit with `git rev-parse HEAD`
- While waiting it polls every few seconds and prints `Waiting for new commit... (Ctrl+C to stop)`
- `Ctrl+C` requests a graceful stop and prints the final summary after the current round finishes

### Strategy Modification Guidelines for AI Agents

When modifying `backtest/strategy.py` in auto mode:

1. Read current `PARAMS` and signal definitions before changing anything
2. Choose exactly ONE change per experiment:
   - adjust a single parameter value
   - add or remove one signal function
   - modify one signal threshold
   - change one position-sizing rule
3. Commit with a descriptive message: `git commit -m "exp<N>: <what changed>"`
4. Do NOT modify `prepare.py`, `backtest.py`, `fetch_data.py`, or any file outside `backtest/strategy.py`

## The Experiment Loop

LOOP FOREVER:

1. `git status` — ensure clean working tree
2. Modify `strategy.py` with ONE experimental idea
3. `git add backtest/strategy.py && git commit -m "exp<N>: <description>"`
4. `uv run backtest/run_experiment.py --description "<description>"`
   - This runs CV backtest, parses score, compares to best, keeps or reverts automatically
   - Exit code: 0 = improved (kept), 1 = worse (reverted), 2 = crashed (reverted)
5. Check `results.tsv` for the recorded result
6. If reverted: think about why, try a different approach
7. Go to step 1

With `backtest/orchestrator.py`, the same loop is managed automatically across many rounds. In `manual` mode the orchestrator waits for the next commit; in `auto` mode it also invokes the AI CLI before each round.

### Manual Loop (without run_experiment.py)

```bash
# Step 3: commit
git add backtest/strategy.py
git commit -m "exp<N>: <description>"

# Step 4: run CV
uv run backtest/backtest.py --mode cv > run.log 2>&1

# Step 5: check score
grep "^cv_score:" run.log

# Step 6: decide
# If improved → keep, record in results.tsv
# If worse → git reset --hard HEAD~1, record in results.tsv
```

## Output Format

CV backtest prints these lines (grep targets):

```
cv_score:           X.XXXXXX    ← PRIMARY metric
mean_score:         X.XXXXXX
std_score:          X.XXXXXX
min_fold_score:     X.XXXXXX
```

Per-fold detail:
```
Fold N [YYYY-MM-DD ~ YYYY-MM-DD]
  score:      X.XXXX
  sharpe:     X.XX
  return:     X.XX%
  max_dd:     X.XX%
  trades:     N
```

## Scoring

### Per-Fold Score (from prepare.py)
```python
score = result.sharpe
if result.max_drawdown_pct > 20:
    score -= (result.max_drawdown_pct - 20) * 0.1
if result.num_trades < 10:
    score -= 1.0
```

### CV Score (aggregated)
```python
cv_score = mean(fold_scores) - 0.5 * std(fold_scores) - catastrophic_penalty
# catastrophic_penalty: +1.0 for each fold with score < -2.0
```

Higher is better. Penalizes high variance across folds and catastrophic individual folds.

## CV Folds

```
Fold 1: Train [2024-04-01 ~ 2025-03-31]  Val [2025-04-01 ~ 2025-06-30]
Fold 2: Train [2024-04-01 ~ 2025-06-30]  Val [2025-07-01 ~ 2025-09-30]
Fold 3: Train [2024-04-01 ~ 2025-09-30]  Val [2025-10-01 ~ 2025-12-31]
Fold 4: Train [2024-04-01 ~ 2025-12-31]  Val [2026-01-01 ~ 2026-03-22]
```

- **CV mode (`--mode cv`):** Used for all experiment scoring. Must generalize across 4 time periods.
- **Single mode (default):** For debugging only. Do NOT use for experiment decisions.

## results.tsv

8-column TSV (tab-separated). Do NOT modify existing rows.

```
experiment  cv_score  mean  std  min_fold  test_score  status  description
```

- `test_score`: `NA` during experimentation (evaluated at the end only)
- `status`: `keep` | `revert` | `crash`

## Strategy Interface

```python
class Strategy:
    def on_bar(self, bar_data: dict[str, BarData], portfolio: PortfolioState) -> list[Signal]:
        ...
```

### BarData
- `symbol`, `date`, `open`, `high`, `low`, `close`, `volume`, `value`
- `history` — DataFrame with last 200 bars (including current)

### Signal
```python
Signal(symbol="BTC", action="buy", weight=0.15, reason="RSI < 30")
```
- **Buy:** `weight` = target portfolio weight (0–1)
- **Sell:** `weight` = fraction of position to sell (1.0 = full liquidation)

### PortfolioState
- `cash`, `positions`, `avg_prices`, `position_dates`, `equity`, `date`, `trade_log`

## Current Strategy Summary (exp204)

Dual RSI mean-reversion with multi-signal voting and per-symbol position sizing.

- 6 bull signals (dual RSI oversold, MACD histogram, BB lower, EMA cross, momentum, volume)
- 5 bear signals (MACD negative, BB upper, EMA cross down, momentum negative, RSI high)
- `MIN_VOTES=4` for buy, `MIN_SELL_VOTES=2` for sell
- Regime gates: falling market block, overheated market filter, trend trap filter
- Per-symbol position sizes (many zeroed out through 204 experiments)
- Stop-loss 2%, max holding 21 days, cooldown 15 days after stop-loss

## Strategy Research Directions

### Tier 1 — Micro-Optimization (Highest Probability)
Most zero-weight positions were turned off individually. Try:
- **Batch re-enable with tiny weights** — some zeroed signals might help in combination
- **Fine-tune non-zero weights** — AVAX trend (0.04) and strong reversion (0.15) have room
- **Adjust MIN_VOTES threshold** — try 3 instead of 4 (more signals fire)
- **Stop-loss refinement** — 2% is tight; try 3% or ATR-based stops
- **Cooldown period tuning** — 15 days may be too long, try 7–10
- **Holding period** — 21 days max; try 14 or 28

### Tier 2 — Signal Enhancement (Medium Risk)
- **RSI divergence** — price makes new low but RSI doesn't → stronger buy signal
- **Volume-weighted RSI** — weight RSI calculation by volume
- **Adaptive RSI thresholds** — lower the oversold threshold in trending markets
- **MACD signal line cross** — add as a bull/bear signal
- **ATR-based position sizing** — lower weight when ATR is high (volatile)
- **Correlation filter** — reduce position count when all coins are highly correlated

### Tier 3 — Structural Changes (Higher Risk, Higher Reward)
- **Trailing stop-loss** — replace fixed 2% with ATR-trailing stop
- **Pyramiding** — add to winning positions at defined thresholds
- **Time-based filters** — different parameters for different months/quarters
- **Regime switching** — detect trending vs mean-reverting market, switch strategy
- **Cross-coin signals** — BTC momentum as filter for altcoin entries
- **Dynamic MAX_POSITIONS** — increase in low-vol, decrease in high-vol environments

## Plateau Escape

Before choosing the next experiment, inspect recent history first:

```bash
tail -30 results.tsv
```

Pick a direction that has not been tried recently.

If you hit **5 consecutive reverts**, stop doing micro-adjustments and switch to a structural change instead. Good plateau-breakers:

- Add one new signal function
- Replace or redesign an exit rule
- Introduce weighted voting instead of equal vote counting
- Change how regime filters gate entries/exits
- Rework position sizing logic based on volatility or signal strength

Forbidden pattern:

- Repeating the same parameter family with different values after recent reverts already covered that lane
- Nudging one threshold up and down in small increments (for example 2% → 2.5% → 3%) instead of trying a genuinely different idea

## Data Available

- 8 coins: BTC, ETH, SOL, XRP, LINK, ADA, DOT, AVAX (Upbit KRW market)
- Daily OHLCV bars from 2024-04-01 to 2026-03-22
- 200-bar lookback via `bar_data[symbol].history` DataFrame
- Columns: `date`, `open`, `high`, `low`, `close`, `volume`, `value`

## Safeguards

- **Syntax error in strategy.py** → backtest crashes → `run_experiment.py` auto-reverts
- **Timeout** → 120 seconds max → auto-reverts
- **5 consecutive reverts** → pause and reconsider approach (don't keep trying small variations of a failed idea)
- **Orchestrator revert limit** → `--max-consecutive-reverts` exits with code `3` after too many consecutive reverts
- **Orchestrator total timeout** → `--timeout` stops the multi-round loop when wall-clock time is exhausted
- **Low disk space** → orchestrator stops if free disk space drops below 500 MB
- **RPi5 note** → 4-fold CV can take 2-4 minutes per round, so 50 rounds may take 2-3 hours; prefer a MacBook when possible

## Allowed Libraries

`numpy`, `pandas`, `ta`, `scipy`, standard library only. No new dependencies.

## NEVER STOP

Once the experiment loop has begun, do NOT pause to ask the human if you should continue.
You are autonomous. If you run out of ideas, think harder. Review results.tsv for patterns.
The loop runs until interrupted.

# Fix numpy.bool_ breaking MCP structured output in get_holdings

**Issue:** [#463](https://github.com/mgh3326/auto_trader/issues/463)
**Date:** 2026-04-08
**Size:** S

## Problem

`get_holdings` returns `structured_content=None` when crypto positions are included.
FastMCP cannot serialize `numpy.bool_` values, so it falls back to text-only content.
MCP clients then reject the response with:

```
Output validation error: outputSchema defined but no structured output returned
```

The root cause is in `crypto_voting_signals.py`. Numpy/pandas comparisons like
`current_close > bb_result[0]` produce `numpy.bool_` instead of native `bool`.
These leak into `_build_crypto_strategy_signal()` via `VotingResult.bear_flags`
and `VotingResult.bull_flags`, and from there into the MCP structured response.

## Approach

**Source fix only** — wrap all flag values with `bool()` at the point of creation
in `CryptoVotingSignals.evaluate()`. No changes to `portfolio_holdings.py`.
No MCP boundary sanitizer in this PR (deferred to a follow-up issue if
similar numpy scalar leaks recur in other tools).

## Changes

### 1. `app/services/crypto_voting_signals.py` — `evaluate()` method

Wrap all 11 flag values and 2 signal booleans with `bool()`:

**bull_flags (6 flags):**

| Flag | Current | After |
|------|---------|-------|
| `dual_rsi_oversold` | `(rsi_slow <= RSI_OVERSOLD and ...)` | `bool(rsi_slow <= RSI_OVERSOLD and ...)` |
| `macd_histogram_positive` | `(macd_result is not None and macd_result[2] > 0)` | `bool(...)` |
| `close_below_bb_lower` | `(bb_result is not None and current_close < bb_result[2])` | `bool(...)` |
| `ema_fast_above_slow` | `(ema_fast is not None and ... and ema_fast[-1] > ema_slow[-1])` | `bool(...)` |
| `momentum_positive` | `momentum is not None and momentum > 0` | `bool(...)` |
| `volume_above_avg` | `(avg_volume is not None and current_volume > avg_volume * VOLUME_THRESHOLD)` | `bool(...)` |

**bear_flags (5 flags):**

| Flag | Current | After |
|------|---------|-------|
| `macd_histogram_negative` | `(macd_result is not None and macd_result[2] < 0)` | `bool(...)` |
| `close_above_bb_upper` | `(bb_result is not None and current_close > bb_result[0])` | `bool(...)` |
| `ema_fast_below_slow` | `(ema_fast is not None and ... and ema_fast[-1] < ema_slow[-1])` | `bool(...)` |
| `momentum_negative` | `momentum is not None and momentum < 0` | `bool(...)` |
| `rsi_slow_high` | `rsi_slow > RSI_EXIT` | `bool(...)` |

**buy_signal / sell_signal:**

```python
buy_signal=bool(bull_votes >= MIN_VOTES),
sell_signal=bool(bear_votes >= MIN_SELL_VOTES),
```

### 2. No changes to `app/mcp_server/tooling/portfolio_holdings.py`

The source fix ensures all values reaching `_build_crypto_strategy_signal()` are
already native `bool`. No code changes needed in the MCP layer.

### 3. Regression tests

**Layer 1: Unit test** in `tests/test_crypto_voting_signals.py`

New test `test_all_flags_are_native_bool`:
- Call `evaluator.evaluate()` with data that activates indicators
- Assert `type(v) is bool` for every value in `bull_flags` and `bear_flags`
- Assert `type(result.buy_signal) is bool` and `type(result.sell_signal) is bool`
- Use `type(v) is bool` (strict identity check), not `isinstance(v, bool)`
  — `isinstance(numpy.bool_, bool)` is `False` on current numpy, but strict
  checking is more future-proof and documents the intent clearly

**Layer 2: MCP structured output test** in `tests/test_mcp_portfolio_tools.py`

New test `test_get_holdings_crypto_strategy_signal_native_types`:
- Set up a crypto position with `profit_rate < 0` and mock OHLCV data producing
  voting results with sell_signal (to trigger the `bear_vote_exit` path that
  exposes `bear_flags` in the response)
- Call `get_holdings(account="upbit", market="crypto")` via the handler
- Assert `strategy_signal` exists and `bear_flags` values are `type(v) is bool`
- Assert `json.dumps(result)` succeeds (supplementary — catches any other
  non-serializable types)

**Layer 3: FastMCP structured output test** in `tests/test_mcp_portfolio_tools.py`

New test `test_get_holdings_crypto_structured_output_survives_fastmcp`:
- Register `get_holdings` on a real `FastMCP` instance (not `DummyMCP`)
- Mock dependencies so the handler returns a crypto position with strategy signals
- Call `mcp.call_tool("get_holdings", {"account": "upbit", "market": "crypto"})`
- Assert `result.structured_content is not None`
- This validates the actual serialization path that broke in production

## Out of scope

- MCP boundary sanitizer (numpy -> native type coercion at the MCP layer)
  - Worth adding if similar leaks recur; track as a separate issue
- `VotingResult.__post_init__` bulk conversion
- `VotingResult.to_dict()` modification
- Changes to `portfolio_holdings.py`

## Test plan

- [ ] `uv run pytest tests/test_crypto_voting_signals.py -v -k "native_bool"` passes
- [ ] `uv run pytest tests/test_mcp_portfolio_tools.py -v -k "native_types or structured_output_survives"` passes
- [ ] `make test` — full suite green
- [ ] `make lint` — no ruff/ty issues

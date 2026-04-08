# Fix numpy.bool_ MCP Structured Output — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix `get_holdings` MCP structured output failure when crypto strategy signals contain `numpy.bool_` values.

**Architecture:** Wrap all flag comparisons in `CryptoVotingSignals.evaluate()` with `bool()` to produce native Python booleans. Add 3-layer regression tests: unit type check, pre-FastMCP handler payload, and FastMCP end-to-end structured output.

**Tech Stack:** Python, FastMCP 3.1.0, numpy, pytest

**Spec:** `docs/superpowers/specs/2026-04-08-fix-numpy-bool-mcp-structured-output.md`

---

### Task 1: Write failing unit test for native bool types

**Files:**
- Modify: `tests/test_crypto_voting_signals.py:64` (add test after `test_result_has_all_fields`)

- [ ] **Step 1: Add the regression test**

Add this test method at the end of the `TestCryptoVotingSignals` class (after line 71):

```python
    def test_all_flags_are_native_bool(self, evaluator):
        """Regression #463: numpy.bool_ breaks FastMCP structured output."""
        # Uptrend then reversal — activates most indicators
        closes = list(np.linspace(100, 200, 40)) + list(np.linspace(200, 130, 10))
        df = _make_ohlcv_df(closes)
        result = evaluator.evaluate(df)
        assert result is not None
        for key, value in result.bull_flags.items():
            assert type(value) is bool, f"bull_flags[{key}] is {type(value)}, not bool"
        for key, value in result.bear_flags.items():
            assert type(value) is bool, f"bear_flags[{key}] is {type(value)}, not bool"
        assert type(result.buy_signal) is bool, "buy_signal is not native bool"
        assert type(result.sell_signal) is bool, "sell_signal is not native bool"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_crypto_voting_signals.py::TestCryptoVotingSignals::test_all_flags_are_native_bool -v`

Expected: FAIL with `bear_flags[close_above_bb_upper] is <class 'numpy.bool_'>, not bool` or similar.

---

### Task 2: Fix source — wrap all flags with bool()

**Files:**
- Modify: `app/services/crypto_voting_signals.py:102-151`

- [ ] **Step 1: Wrap bull_flags with bool()**

Replace lines 103-123 with:

```python
        bull_flags = {
            "dual_rsi_oversold": bool(
                rsi_slow <= RSI_OVERSOLD
                and rsi_fast is not None
                and rsi_fast <= RSI_OVERSOLD
            ),
            "macd_histogram_positive": bool(
                macd_result is not None and macd_result[2] > 0
            ),
            "close_below_bb_lower": bool(
                bb_result is not None and current_close < bb_result[2]
            ),
            "ema_fast_above_slow": bool(
                ema_fast is not None
                and ema_slow is not None
                and ema_fast[-1] > ema_slow[-1]
            ),
            "momentum_positive": bool(momentum is not None and momentum > 0),
            "volume_above_avg": bool(
                avg_volume is not None
                and current_volume > avg_volume * VOLUME_THRESHOLD
            ),
        }
```

- [ ] **Step 2: Wrap bear_flags with bool()**

Replace lines 127-139 with:

```python
        bear_flags = {
            "macd_histogram_negative": bool(
                macd_result is not None and macd_result[2] < 0
            ),
            "close_above_bb_upper": bool(
                bb_result is not None and current_close > bb_result[0]
            ),
            "ema_fast_below_slow": bool(
                ema_fast is not None
                and ema_slow is not None
                and ema_fast[-1] < ema_slow[-1]
            ),
            "momentum_negative": bool(momentum is not None and momentum < 0),
            "rsi_slow_high": bool(rsi_slow > RSI_EXIT),
        }
```

- [ ] **Step 3: Wrap buy_signal and sell_signal with bool()**

Replace lines 149-150 in the `VotingResult(...)` constructor call:

```python
            buy_signal=bool(bull_votes >= MIN_VOTES),
            sell_signal=bool(bear_votes >= MIN_SELL_VOTES),
```

- [ ] **Step 4: Run the unit test to verify it passes**

Run: `uv run pytest tests/test_crypto_voting_signals.py -v`

Expected: ALL tests pass, including `test_all_flags_are_native_bool`.

- [ ] **Step 5: Commit**

```bash
git add app/services/crypto_voting_signals.py tests/test_crypto_voting_signals.py
git commit -m "fix: wrap voting signal flags with bool() to fix MCP structured output (#463)"
```

---

### Task 3: Write Layer 2 — pre-FastMCP handler payload test

**Files:**
- Modify: `tests/test_mcp_portfolio_tools.py` (append new test at end of file)

- [ ] **Step 1: Add the pre-FastMCP handler payload test**

Append at the end of `tests/test_mcp_portfolio_tools.py`:

```python
@pytest.mark.asyncio
async def test_get_holdings_crypto_strategy_signal_native_types(monkeypatch):
    """Regression #463: strategy_signal values must be native JSON-safe types."""
    import json

    tools = build_tools()

    # profit_rate = -2.0 (loss, but not stop-loss at -4.5)
    # This + sell_signal=True triggers bear_vote_exit, which exposes bear_flags
    mocked_positions = [
        {
            "symbol": "KRW-BTC",
            "name": "Bitcoin",
            "instrument_type": "crypto",
            "market": "crypto",
            "account": "upbit",
            "broker": "upbit",
            "account_name": "Upbit Main",
            "quantity": 0.1,
            "avg_buy_price": 50000000.0,
            "current_price": 49000000.0,
            "evaluation_amount": 4900000.0,
            "profit_loss": -100000.0,
            "profit_rate": -2.0,
        }
    ]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "crypto", None)),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_get_indicators_impl",
        AsyncMock(
            return_value={"symbol": "KRW-BTC", "indicators": {"rsi": {"14": 55.0}}}
        ),
    )

    # OHLCV: uptrend then sharp reversal -> produces sell_signal=True (bear_votes >= 2)
    import numpy as np

    closes = list(np.linspace(100, 200, 40)) + list(np.linspace(200, 130, 10))
    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1000.0] * 50,
        }
    )
    _patch_runtime_attr(
        monkeypatch,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(return_value=df),
    )

    result = await tools["get_holdings"](account="upbit", market="crypto")
    btc_position = result["accounts"][0]["positions"][0]

    signal = btc_position.get("strategy_signal")
    assert signal is not None, "strategy_signal missing from crypto position"
    assert signal["reason"] == "bear_vote_exit"

    # Core assertion: all bear_flags values must be native bool
    for key, value in signal["bear_flags"].items():
        assert type(value) is bool, f"bear_flags[{key}] is {type(value)}, not bool"

    # Supplementary: entire result must be JSON-serializable
    json.dumps(result)
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_portfolio_tools.py::test_get_holdings_crypto_strategy_signal_native_types -v`

Expected: PASS

---

### Task 4: Write Layer 3 — FastMCP end-to-end structured output test

**Files:**
- Modify: `tests/test_mcp_portfolio_tools.py` (append new test at end of file)

- [ ] **Step 1: Add the FastMCP structured output test**

Append at the end of `tests/test_mcp_portfolio_tools.py`:

```python
@pytest.mark.asyncio
async def test_get_holdings_crypto_structured_output_survives_fastmcp(monkeypatch):
    """Regression #463: FastMCP must produce structured output, not text-only fallback."""
    from fastmcp import FastMCP

    from app.mcp_server.tooling.registry import register_all_tools

    mcp = FastMCP("test")
    register_all_tools(mcp)

    # Same setup as Layer 2: loss position + reversal OHLCV -> bear_vote_exit
    mocked_positions = [
        {
            "symbol": "KRW-BTC",
            "name": "Bitcoin",
            "instrument_type": "crypto",
            "market": "crypto",
            "account": "upbit",
            "broker": "upbit",
            "account_name": "Upbit Main",
            "quantity": 0.1,
            "avg_buy_price": 50000000.0,
            "current_price": 49000000.0,
            "evaluation_amount": 4900000.0,
            "profit_loss": -100000.0,
            "profit_rate": -2.0,
        }
    ]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "crypto", None)),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_get_indicators_impl",
        AsyncMock(
            return_value={"symbol": "KRW-BTC", "indicators": {"rsi": {"14": 55.0}}}
        ),
    )

    import numpy as np

    closes = list(np.linspace(100, 200, 40)) + list(np.linspace(200, 130, 10))
    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1000.0] * 50,
        }
    )
    _patch_runtime_attr(
        monkeypatch,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(return_value=df),
    )

    tool_result = await mcp.call_tool(
        "get_holdings", {"account": "upbit", "market": "crypto"}
    )

    # Core assertion: structured output must survive FastMCP serialization
    assert tool_result.structured_content is not None, (
        "structured_content is None — FastMCP failed to serialize the response. "
        "This likely means a non-JSON-safe type (e.g. numpy.bool_) leaked through."
    )
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_portfolio_tools.py::test_get_holdings_crypto_structured_output_survives_fastmcp -v`

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_mcp_portfolio_tools.py
git commit -m "test: add regression tests for MCP structured output with crypto signals (#463)"
```

---

### Task 5: Final verification

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest tests/test_crypto_voting_signals.py tests/test_mcp_portfolio_tools.py -v`

Expected: ALL tests pass.

- [ ] **Step 2: Run lint**

Run: `make lint`

Expected: No errors.

- [ ] **Step 3: Run full project tests**

Run: `make test`

Expected: All tests pass.

- [ ] **Step 4: Final commit (if lint required formatting changes)**

Only if formatting changes were needed:

```bash
git add -u
git commit -m "style: fix formatting in regression tests (#463)"
```

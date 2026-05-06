# Refactor Float Comparisons Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor manual float comparisons and direct float equalities in test files to use `pytest.approx` for better robustness and to address SonarCloud rule python:S1244.

**Architecture:** Use `pytest.approx` for all float comparisons in the specified test files. Handle `Decimal` types correctly where they appear.

**Tech Stack:** Python, pytest

---

### Task 1: Refactor `tests/test_mcp_indicator_math.py`

**Files:**
- Modify: `tests/test_mcp_indicator_math.py`

- [ ] **Step 1: Replace manual abs comparisons with pytest.approx**

```python
# Around line 217
assert result["histogram"] == pytest.approx(expected_hist, abs=0.01)

# Around line 258
assert bollinger["middle"] == pytest.approx(sma["20"], abs=0.01)

# Around line 608
assert result["signal"] == pytest.approx(expected_signal, abs=0.01)
```

- [ ] **Step 2: Run tests to verify**

Run: `uv run pytest tests/test_mcp_indicator_math.py`

---

### Task 2: Refactor `tests/test_n8n_daily_brief_service.py`

**Files:**
- Modify: `tests/test_n8n_daily_brief_service.py`

- [ ] **Step 1: Replace manual abs comparisons with pytest.approx**

```python
# Around line 319
assert us["pnl_pct"] == pytest.approx(6.67, abs=1.0)

# Around line 344
assert kr["pnl_pct"] == pytest.approx(7.14, abs=1.0)

# Around line 368
assert crypto["pnl_pct"] == pytest.approx(10.0, abs=1.0)

# Around line 394
assert us["pnl_pct"] == pytest.approx(5.56, abs=1.0)

# Around line 456
assert us["pnl_pct"] == pytest.approx(-1.96, abs=0.5)
```

- [ ] **Step 2: Run tests to verify**

Run: `uv run pytest tests/test_n8n_daily_brief_service.py`

---

### Task 3: Refactor `tests/test_mcp_fundamentals_tools.py`

**Files:**
- Modify: `tests/test_mcp_fundamentals_tools.py`

- [ ] **Step 1: Replace direct float equalities and abs comparisons with pytest.approx**
Note: This file has many occurrences. I will target the ones mentioned and others found.

```python
# Around line 247
assert rec["rsi14"] == pytest.approx(45.8)

# Around line 258
assert rec["rsi14"] == pytest.approx(0.0)

# Around line 341
assert result["indicators"]["rsi"]["14"] == pytest.approx(45.8)

# Around line 343
assert result["recommendation"]["rsi14"] == pytest.approx(45.8)

# And many others in TestGetValuation, TestGetMarketIndex, etc.
# example:
# assert result["per"] == pytest.approx(12.5)
```

- [ ] **Step 2: Run tests to verify**

Run: `uv run pytest tests/test_mcp_fundamentals_tools.py`

---

### Task 4: Refactor `tests/test_portfolio_overview_service.py`

**Files:**
- Modify: `tests/test_portfolio_overview_service.py`

- [ ] **Step 1: Replace manual abs comparisons with pytest.approx**

```python
# Around line 1306
assert result[0]["avg_price"] == pytest.approx(200000.0 / 1350.0, abs=0.01)

# Around line 1332
assert result[0]["avg_price"] == pytest.approx(195000.0 / 1300.0, abs=0.01)

# Around line 1351
assert result["profit_loss"] == pytest.approx(15 * 120.0 - cost_basis, abs=0.01)
```

- [ ] **Step 2: Run tests to verify**

Run: `uv run pytest tests/test_portfolio_overview_service.py`

---

### Task 5: Refactor `tests/test_trading_integration.py`

**Files:**
- Modify: `tests/test_trading_integration.py`

- [ ] **Step 1: Replace manual abs comparisons with pytest.approx**

```python
# Replace matches like:
# assert abs(result - expected) < 0.01
# with:
# assert result == pytest.approx(expected, abs=0.01)
```

- [ ] **Step 2: Run tests to verify**

Run: `uv run pytest tests/test_trading_integration.py`

---

### Task 6: Refactor `tests/test_naver_finance.py`

**Files:**
- Modify: `tests/test_naver_finance.py`

- [ ] **Step 1: Replace manual abs comparisons with pytest.approx**

```python
# Around line 90, 1017
assert result["dividend_yield"] == pytest.approx(0.02, abs=0.001)

# Around line 700, 891
assert consensus["upside_pct"] == pytest.approx(16.67, abs=0.01)

# Around line 1049
assert result["current_position_52w"] == pytest.approx(0.83, abs=0.01)
```

- [ ] **Step 2: Run tests to verify**

Run: `uv run pytest tests/test_naver_finance.py`

---

### Task 7: Refactor `tests/services/test_portfolio_overview_currency.py`

**Files:**
- Modify: `tests/services/test_portfolio_overview_currency.py`

- [ ] **Step 1: Replace manual abs comparisons with pytest.approx**

```python
# Around line 65
assert position["avg_price"] == pytest.approx(150.0, abs=0.01)

# Around line 70
assert actual_cost_basis == pytest.approx(expected_cost_basis, abs=0.01)

# Around line 74
assert position["profit_rate"] == pytest.approx(0.333, abs=0.01)

# Around line 212, 215
assert holding.toss_avg_price == pytest.approx(150.0, abs=0.01)
assert holding.combined_avg_price == pytest.approx(150.0, abs=0.01)

# Around line 219
assert holding.profit_rate == pytest.approx(expected_profit_rate, abs=0.01)
```

- [ ] **Step 2: Run tests to verify**

Run: `uv run pytest tests/services/test_portfolio_overview_currency.py`

---

### Task 8: Refactor `tests/services/test_pending_reconciliation_service.py`

**Files:**
- Modify: `tests/services/test_pending_reconciliation_service.py`

- [ ] **Step 1: Replace manual abs comparisons with pytest.approx**

```python
# Around line 168
assert item.gap_pct == pytest.approx(Decimal("0.2857"), abs=Decimal("0.001"))
```

- [ ] **Step 2: Run tests to verify**

Run: `uv run pytest tests/services/test_pending_reconciliation_service.py`

---

### Task 9: Final Formatting and Cleanup

- [ ] **Step 1: Run format and lint**

Run: `make format && make lint`

- [ ] **Step 2: Final test run**

Run: `make test-unit`

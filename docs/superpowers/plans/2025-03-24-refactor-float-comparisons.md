# Refactor Float Comparisons in `tests/test_trading_integration.py` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace manual absolute difference checks with `pytest.approx` in `tests/test_trading_integration.py`.

**Architecture:** Use `pytest.approx` with explicit `abs` tolerance for floating point assertions.

**Tech Stack:** Python, Pytest.

---

### Task 1: Refactor `TestMergedPortfolioService`

**Files:**
- Modify: `tests/test_trading_integration.py`

- [ ] **Step 1: Replace manual comparison in `test_calculate_combined_avg_basic`**

```python
        # OLD
        assert abs(result - expected) < 0.01
        # NEW
        assert result == pytest.approx(expected, abs=0.01)
```

- [ ] **Step 2: Replace manual comparison in `test_calculate_combined_avg_three_brokers`**

```python
        # OLD
        assert abs(result - expected) < 0.01
        # NEW
        assert result == pytest.approx(expected, abs=0.01)
```

- [ ] **Step 3: Run tests and verify**

Run: `uv run pytest tests/test_trading_integration.py::TestMergedPortfolioService -v`
Expected: PASS

### Task 2: Refactor `TestReferencePrices` and `TestTradingPriceService`

**Files:**
- Modify: `tests/test_trading_integration.py`

- [ ] **Step 1: Replace manual comparison in `TestReferencePrices.test_reference_prices_both_brokers`**

```python
        # OLD
        assert abs(data["combined_avg"] - 73666.67) < 0.01
        # NEW
        assert data["combined_avg"] == pytest.approx(73666.67, abs=0.01)
```

- [ ] **Step 2: Replace manual comparison in `TestTradingPriceService.test_buy_price_with_combined_avg`**

```python
        # OLD
        assert abs(result.price - 73666.67) < 0.01
        # NEW
        assert result.price == pytest.approx(73666.67, abs=0.01)
```

- [ ] **Step 3: Replace manual comparison in `TestTradingPriceService.test_buy_price_with_lowest_minus_percent`**

```python
        # OLD
        assert abs(result.price - expected) < 0.01
        # NEW
        assert result.price == pytest.approx(expected, abs=0.01)
```

- [ ] **Step 4: Replace manual comparison in `TestTradingPriceService.test_sell_price_with_kis_avg_plus`**

```python
        # OLD
        assert abs(result.price - expected) < 0.01
        # NEW
        assert result.price == pytest.approx(expected, abs=0.01)
```

- [ ] **Step 5: Replace manual comparison in `TestTradingPriceService.test_sell_price_with_toss_avg_plus`**

```python
        # OLD
        assert abs(result.price - expected) < 0.01
        # NEW
        assert result.price == pytest.approx(expected, abs=0.01)
```

- [ ] **Step 6: Replace manual comparison in `TestTradingPriceService.test_sell_price_with_combined_avg_plus`**

```python
        # OLD
        assert abs(result.price - expected) < 1  # 소수점 반올림 오차 허용
        # NEW
        assert result.price == pytest.approx(expected, abs=1)
```

- [ ] **Step 7: Run tests and verify**

Run: `uv run pytest tests/test_trading_integration.py::TestReferencePrices -v && uv run pytest tests/test_trading_integration.py::TestTradingPriceService -v`
Expected: PASS

### Task 3: Refactor `TestExpectedProfit`, `TestGetReferencePricesIntegration`, and `TestRequirementsVerification`

**Files:**
- Modify: `tests/test_trading_integration.py`

- [ ] **Step 1: Replace manual comparison in `TestExpectedProfit.test_expected_profit_percent`**

```python
        # OLD
        assert abs(result["based_on_kis_avg"].percent - expected_percent) < 0.01
        # NEW
        assert result["based_on_kis_avg"].percent == pytest.approx(expected_percent, abs=0.01)
```

- [ ] **Step 2: Replace manual comparison in `TestGetReferencePricesIntegration.test_get_reference_prices_both_brokers`**

```python
        # OLD
        assert abs(ref.combined_avg - expected_combined) < 0.01
        # NEW
        assert ref.combined_avg == pytest.approx(expected_combined, abs=0.01)
```

- [ ] **Step 3: Replace manual comparison in `TestRequirementsVerification.test_requirement_buy_uses_all_reference_prices`**

```python
        # OLD
        assert abs(r3.price - 73666.67) < 0.01
        # NEW
        assert r3.price == pytest.approx(73666.67, abs=0.01)
```

- [ ] **Step 4: Replace manual comparison in `TestRequirementsVerification.test_requirement_sell_price_strategies` (3 occurrences)**

```python
        # OLD
        assert abs(r1.price - 74000 * 1.05) < 0.01
        assert abs(r2.price - 73000 * 1.10) < 0.01
        assert abs(r3.price - 73666.67 * 1.03) < 1
        # NEW
        assert r1.price == pytest.approx(74000 * 1.05, abs=0.01)
        assert r2.price == pytest.approx(73000 * 1.10, abs=0.01)
        assert r3.price == pytest.approx(73666.67 * 1.03, abs=1)
```

- [ ] **Step 5: Run all tests and verify**

Run: `uv run pytest tests/test_trading_integration.py -v`
Expected: PASS

- [ ] **Step 6: Format and Lint**

Run: `make format && make lint`
Expected: Success

- [ ] **Step 7: Commit**

```bash
git add tests/test_trading_integration.py
git commit -m "test: refactor float comparisons to use pytest.approx in test_trading_integration.py"
```

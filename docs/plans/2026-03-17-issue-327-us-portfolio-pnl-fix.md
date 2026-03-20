# Issue #327: US Portfolio PnL -96.8% Fix

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the `/api/n8n/daily-brief` endpoint that displays US portfolio PnL as -96.8% due to a currency mismatch in the PnL recalculation logic.

**Architecture:** The daily brief's `_build_portfolio_summary()` recalculates PnL from `avg_price * quantity` vs `evaluation`. For US stocks, `avg_price` (from KIS `pchs_avg_pric` in USD, or manual holdings in KRW) and `evaluation` (in USD) can be in different currencies when manual holdings are present. The fix replaces this raw recalculation with a derivation from each position's pre-computed `profit_rate` and `evaluation`, which are currency-consistent.

**Tech Stack:** Python 3.13+, pytest, FastAPI, KIS API

---

## Bug Analysis

### Data Flow
```
GET /api/n8n/daily-brief
  → fetch_daily_brief()
    → _get_portfolio_overview()
      → PortfolioOverviewService.get_overview()
        → _collect_kis_components()        # KIS live holdings
        → _collect_manual_components()      # Toss/Samsung manual holdings
        → _aggregate_positions()            # Combines sources per-symbol
    → _build_portfolio_summary()            # ← BUG HERE: recalculates PnL
```

### Root Cause

**`_build_portfolio_summary()`** in `app/services/n8n_daily_brief_service.py:217-224`:

```python
total_eval = sum(float(p.get("evaluation") or 0) for p in market_positions)     # USD
total_cost = sum(
    float(p.get("avg_price") or 0) * float(p.get("quantity") or 0)             # MIXED!
    for p in market_positions
)
pnl_pct = ((total_eval - total_cost) / total_cost * 100) if total_cost > 0 else None
```

For US stocks with manual holdings (Toss/Samsung):
- `avg_price` from KIS: ~$150 (USD, from `pchs_avg_pric`)
- `avg_price` from manual: ~₩200,000 (KRW, stored in `manual_holdings.avg_price`)
- `evaluation`: ~$150 × qty (USD, from `ovrs_stck_evlu_amt` or recalculated)

When `_aggregate_positions()` combines these, the weighted `avg_price` mixes USD and KRW values, making `total_cost` astronomically high relative to `total_eval` (USD), producing ~-96.8% PnL.

### Why KIS-only positions are fine
For positions only from KIS, `pchs_avg_pric` (USD), `now_pric2` (USD), and `ovrs_stck_evlu_amt` (USD) are all in the same currency, so the per-position `profit_rate` from KIS API (`evlu_pfls_rt`) is correct.

---

## Fix Strategy

**Primary Fix (Task 1-2):** Rewrite `_build_portfolio_summary()` to derive portfolio PnL from per-position `profit_rate` and `evaluation` instead of recalculating from `avg_price * quantity`. This is currency-safe because `profit_rate` and `evaluation` are always in the same currency context.

**Secondary Fix (Task 3):** Fix `_aggregate_positions()` so that the position-level `profit_rate` is also correct for mixed-source US positions. Use the KIS-provided `profit_rate` as the authoritative source when available, only recalculating for manual-only positions.

---

### Task 1: Fix `_build_portfolio_summary` PnL calculation (test first)

**Files:**
- Test: `tests/test_n8n_daily_brief_service.py` (create)
- Modify: `app/services/n8n_daily_brief_service.py:201-268`

**Step 1: Write the failing test**

Create `tests/test_n8n_daily_brief_service.py`:

```python
"""Tests for n8n daily brief service portfolio PnL calculation."""
from __future__ import annotations

import pytest

from app.services.n8n_daily_brief_service import _build_portfolio_summary


@pytest.mark.unit
class TestBuildPortfolioSummary:
    """Test _build_portfolio_summary PnL calculation."""

    def _make_overview(self, positions: list[dict]) -> dict:
        return {"positions": positions}

    def test_us_pnl_uses_profit_rate_not_avg_price(self):
        """Regression: Issue #327 - US PnL should not mix KRW/USD avg_price.

        When manual holdings have avg_price in KRW and KIS has USD,
        the aggregated avg_price is nonsensical. The fix uses per-position
        profit_rate and evaluation to derive PnL.
        """
        # Simulate an aggregated US position where avg_price is a
        # mixed KRW/USD weighted average (the broken aggregation output).
        # KIS: 5 shares, avg $150, current $160 → profit_rate ≈ 0.0667
        # Manual: 5 shares, avg ₩200,000 (KRW!), but after aggregation:
        #   avg_price = (5*150 + 5*200000) / 10 = 100,075 (nonsense)
        #   evaluation = 10 * 160 = 1,600 (USD)
        #   profit_rate = (1600 - 1000750) / 1000750 ≈ -0.998 (WRONG from aggregation)
        #
        # But the individual KIS position profit_rate (0.0667) is correct.
        # After our fix, _build_portfolio_summary should use evaluation and
        # profit_rate from positions, not recalculate from avg_price.

        overview = self._make_overview([
            {
                "market_type": "US",
                "symbol": "NVDA",
                "name": "NVIDIA",
                "quantity": 10,
                "avg_price": 100_075,  # Broken mixed avg from aggregation
                "current_price": 160.0,
                "evaluation": 1_600.0,  # 10 * $160, in USD
                "profit_loss": 100.0,   # Correct: $1600 - $1500 = $100
                "profit_rate": 0.0667,  # Correct: from KIS API or proper calc
            },
        ])

        result = _build_portfolio_summary(overview)
        us = result.get("us")
        assert us is not None

        # PnL should be approximately +6.67%, NOT -99.8%
        assert us["pnl_pct"] is not None
        assert us["pnl_pct"] > 0, f"Expected positive PnL, got {us['pnl_pct']}"
        assert abs(us["pnl_pct"] - 6.67) < 1.0

    def test_kr_pnl_still_works(self):
        """KR positions should still calculate PnL correctly."""
        overview = self._make_overview([
            {
                "market_type": "KR",
                "symbol": "005930",
                "name": "삼성전자",
                "quantity": 100,
                "avg_price": 70_000.0,
                "current_price": 75_000.0,
                "evaluation": 7_500_000.0,
                "profit_loss": 500_000.0,
                "profit_rate": 0.0714,
            },
        ])

        result = _build_portfolio_summary(overview)
        kr = result.get("kr")
        assert kr is not None
        assert kr["pnl_pct"] is not None
        assert kr["pnl_pct"] > 0
        assert abs(kr["pnl_pct"] - 7.14) < 1.0

    def test_crypto_pnl_still_works(self):
        """Crypto positions should still calculate PnL correctly."""
        overview = self._make_overview([
            {
                "market_type": "CRYPTO",
                "symbol": "KRW-BTC",
                "name": "BTC",
                "quantity": 0.5,
                "avg_price": 100_000_000.0,
                "current_price": 110_000_000.0,
                "evaluation": 55_000_000.0,
                "profit_loss": 5_000_000.0,
                "profit_rate": 0.10,
            },
        ])

        result = _build_portfolio_summary(overview)
        crypto = result.get("crypto")
        assert crypto is not None
        assert crypto["pnl_pct"] is not None
        assert abs(crypto["pnl_pct"] - 10.0) < 1.0

    def test_position_without_profit_rate_falls_back(self):
        """Positions missing profit_rate should fall back gracefully."""
        overview = self._make_overview([
            {
                "market_type": "US",
                "symbol": "AAPL",
                "name": "Apple",
                "quantity": 5,
                "avg_price": 180.0,
                "current_price": 190.0,
                "evaluation": 950.0,
                "profit_loss": None,
                "profit_rate": None,
            },
        ])

        result = _build_portfolio_summary(overview)
        us = result.get("us")
        assert us is not None
        # When profit_rate is None, fall back to avg_price * qty calculation
        # $950 - $900 = $50, $50/$900 = 5.56%
        assert us["pnl_pct"] is not None
        assert abs(us["pnl_pct"] - 5.56) < 1.0

    def test_zero_evaluation_returns_none_pnl(self):
        """Zero evaluation should not cause division errors."""
        overview = self._make_overview([
            {
                "market_type": "US",
                "symbol": "XYZ",
                "name": "Dead Stock",
                "quantity": 10,
                "avg_price": 50.0,
                "current_price": 0.0,
                "evaluation": 0.0,
                "profit_loss": -500.0,
                "profit_rate": -1.0,
            },
        ])

        result = _build_portfolio_summary(overview)
        us = result.get("us")
        assert us is not None
        # Should handle gracefully, not crash
        assert us["pnl_pct"] == -100.0 or us["pnl_pct"] is None

    def test_multi_position_weighted_pnl(self):
        """Multiple US positions should produce weighted PnL."""
        overview = self._make_overview([
            {
                "market_type": "US",
                "symbol": "NVDA",
                "name": "NVIDIA",
                "quantity": 10,
                "avg_price": 150.0,
                "current_price": 160.0,
                "evaluation": 1_600.0,
                "profit_loss": 100.0,
                "profit_rate": 0.0667,  # +6.67%
            },
            {
                "market_type": "US",
                "symbol": "AAPL",
                "name": "Apple",
                "quantity": 20,
                "avg_price": 180.0,
                "current_price": 170.0,
                "evaluation": 3_400.0,
                "profit_loss": -200.0,
                "profit_rate": -0.0556,  # -5.56%
            },
        ])

        result = _build_portfolio_summary(overview)
        us = result.get("us")
        assert us is not None
        # Total eval: $5,000; Total cost: $1,500 + $3,600 = $5,100; PnL: -1.96%
        # Using profit_rate method: cost_NVDA = 1600/1.0667 ≈ 1500, cost_AAPL = 3400/0.9444 ≈ 3600
        # Total cost ≈ 5100, PnL = (5000 - 5100)/5100 * 100 ≈ -1.96%
        assert us["pnl_pct"] is not None
        assert abs(us["pnl_pct"] - (-1.96)) < 0.5
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_n8n_daily_brief_service.py -v -m unit`
Expected: FAIL — the US PnL test produces ~-99.8% instead of +6.67%

**Step 3: Fix `_build_portfolio_summary` to use profit_rate-based PnL**

Modify `app/services/n8n_daily_brief_service.py:201-268`. Replace the PnL calculation block:

```python
def _build_portfolio_summary(
    overview: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Build per-market portfolio summary from PortfolioOverviewService output."""
    positions = overview.get("positions", [])
    by_market: dict[str, list[dict[str, Any]]] = {}

    for pos in positions:
        market_type = str(pos.get("market_type", "")).upper()
        market_map = {"KR": "kr", "US": "us", "CRYPTO": "crypto"}
        market = market_map.get(market_type, "")
        if market:
            by_market.setdefault(market, []).append(pos)

    result: dict[str, dict[str, Any]] = {}
    for market, market_positions in by_market.items():
        total_eval = sum(float(p.get("evaluation") or 0) for p in market_positions)

        # Derive cost from profit_rate and evaluation to avoid currency mismatch.
        # For US stocks, avg_price may be in KRW (manual holdings) or USD (KIS),
        # but profit_rate and evaluation are always in the same currency context.
        total_cost = 0.0
        has_reliable_cost = False
        for p in market_positions:
            eval_amt = float(p.get("evaluation") or 0)
            rate = p.get("profit_rate")
            if eval_amt > 0 and rate is not None:
                denominator = 1.0 + float(rate)
                if denominator > 0:
                    total_cost += eval_amt / denominator
                    has_reliable_cost = True
                else:
                    # profit_rate == -1.0 means total loss; cost = eval - profit_loss
                    profit_loss = float(p.get("profit_loss") or 0)
                    total_cost += eval_amt - profit_loss
                    has_reliable_cost = True
            elif eval_amt <= 0 and rate is not None and rate <= -1.0:
                # Zero evaluation, total loss — derive cost from profit_loss
                profit_loss = float(p.get("profit_loss") or 0)
                total_cost += -profit_loss if profit_loss < 0 else 0
                has_reliable_cost = True
            else:
                # Fallback: use avg_price * quantity (safe for same-currency markets)
                avg = float(p.get("avg_price") or 0)
                qty = float(p.get("quantity") or 0)
                total_cost += avg * qty

        pnl_pct = (
            ((total_eval - total_cost) / total_cost * 100) if total_cost > 0 else None
        )

        # Top gainers/losers by profit_rate
        sorted_positions = sorted(
            [p for p in market_positions if p.get("profit_rate") is not None],
            key=lambda p: float(p.get("profit_rate") or 0),
            reverse=True,
        )
        top_gainers = [
            {
                "symbol": p["symbol"],
                "change_pct": fmt_pnl(float(p["profit_rate"]) * 100),
            }
            for p in sorted_positions[:3]
            if float(p.get("profit_rate") or 0) > 0
        ]
        top_losers = [
            {
                "symbol": p["symbol"],
                "change_pct": fmt_pnl(float(p["profit_rate"]) * 100),
            }
            for p in reversed(sorted_positions[-3:])
            if float(p.get("profit_rate") or 0) < 0
        ]

        currency = "USD" if market == "us" else "KRW"
        summary: dict[str, Any] = {
            "total_value_fmt": fmt_value(total_eval, currency),
            "pnl_pct": round(pnl_pct, 1) if pnl_pct is not None else None,
            "pnl_fmt": fmt_pnl(round(pnl_pct, 1) if pnl_pct is not None else None),
            "position_count": len(market_positions),
            "top_gainers": top_gainers,
            "top_losers": top_losers,
        }

        if market == "us":
            summary["total_value_usd"] = total_eval
            summary["total_value_krw"] = None
        else:
            summary["total_value_krw"] = total_eval
            summary["total_value_usd"] = None

        result[market] = summary

    return result
```

Key change: Instead of `total_cost = sum(avg_price * quantity)`, we now back-derive cost from `evaluation / (1 + profit_rate)` for each position. This is currency-safe because `evaluation` and `profit_rate` are always computed in the same currency context.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_n8n_daily_brief_service.py -v -m unit`
Expected: ALL PASS

**Step 5: Run existing daily brief tests to verify no regressions**

Run: `uv run pytest tests/test_n8n_daily_brief_api.py -v`
Expected: ALL PASS

**Step 6: Commit**

```bash
git add tests/test_n8n_daily_brief_service.py app/services/n8n_daily_brief_service.py
git commit -m "fix(daily-brief): use profit_rate-based PnL to avoid currency mismatch (#327)"
```

---

### Task 2: Fix `_aggregate_positions` for mixed-source US positions

**Files:**
- Test: `tests/test_portfolio_overview_service.py` (create)
- Modify: `app/services/portfolio_overview_service.py:798-893`

**Step 1: Write the failing test**

Create `tests/test_portfolio_overview_service.py`:

```python
"""Tests for PortfolioOverviewService position aggregation."""
from __future__ import annotations

import pytest

from app.services.portfolio_overview_service import PortfolioOverviewService


@pytest.mark.unit
class TestAggregatePositions:
    """Test _aggregate_positions handles mixed-currency US positions."""

    def _make_service(self) -> PortfolioOverviewService:
        """Create service with a mock DB session."""
        from unittest.mock import MagicMock
        return PortfolioOverviewService(MagicMock())

    def test_us_mixed_source_uses_live_profit_rate(self):
        """Issue #327: Mixed KIS+manual US positions should use KIS profit_rate.

        KIS returns avg_price in USD, manual holdings store avg_price in KRW.
        Aggregation should prefer the live source's profit_rate over recalculating
        from mixed-currency cost_basis.
        """
        service = self._make_service()
        components = [
            # KIS live: NVDA, 5 shares, $150 avg, $160 current
            {
                "market_type": "US",
                "symbol": "NVDA",
                "name": "NVIDIA",
                "account_key": "live:kis",
                "broker": "kis",
                "account_name": "KIS",
                "source": "live",
                "quantity": 5,
                "avg_price": 150.0,        # USD
                "current_price": 160.0,     # USD
                "evaluation": 800.0,        # USD
                "profit_loss": 50.0,        # USD
                "profit_rate": 0.0667,      # Correct from KIS API
            },
            # Manual (Toss): NVDA, 5 shares, ₩200,000 avg (KRW!)
            {
                "market_type": "US",
                "symbol": "NVDA",
                "name": "NVIDIA",
                "account_key": "manual:1",
                "broker": "toss",
                "account_name": "Toss",
                "source": "manual",
                "quantity": 5,
                "avg_price": 200_000.0,     # KRW! (currency mismatch)
                "current_price": 160.0,     # USD (filled by _fill_missing_prices)
                "evaluation": 800.0,        # USD (recalculated)
                "profit_loss": -199_200.0,  # Wrong: 800 - 1_000_000
                "profit_rate": -0.9992,     # Wrong: mixed currencies
            },
        ]

        positions = service._aggregate_positions(components)
        nvda = next(p for p in positions if p["symbol"] == "NVDA")

        # The position profit_rate should NOT be deeply negative
        # With the fix, it should use the live source's profit_rate as basis
        # or at minimum not produce -99% due to currency mismatch
        assert nvda["profit_rate"] > -0.5, (
            f"Expected reasonable profit_rate, got {nvda['profit_rate']}"
        )

    def test_single_source_kr_unchanged(self):
        """KR positions from single source should work as before."""
        service = self._make_service()
        components = [
            {
                "market_type": "KR",
                "symbol": "005930",
                "name": "삼성전자",
                "account_key": "live:kis",
                "broker": "kis",
                "account_name": "KIS",
                "source": "live",
                "quantity": 100,
                "avg_price": 70_000.0,
                "current_price": 75_000.0,
                "evaluation": 7_500_000.0,
                "profit_loss": 500_000.0,
                "profit_rate": 0.0714,
            },
        ]

        positions = service._aggregate_positions(components)
        samsung = next(p for p in positions if p["symbol"] == "005930")
        assert abs(samsung["profit_rate"] - 0.0714) < 0.01

    def test_single_source_us_unchanged(self):
        """US positions from KIS only should work correctly."""
        service = self._make_service()
        components = [
            {
                "market_type": "US",
                "symbol": "AAPL",
                "name": "Apple",
                "account_key": "live:kis",
                "broker": "kis",
                "account_name": "KIS",
                "source": "live",
                "quantity": 10,
                "avg_price": 180.0,       # USD
                "current_price": 190.0,   # USD
                "evaluation": 1_900.0,    # USD
                "profit_loss": 100.0,     # USD
                "profit_rate": 0.0556,    # Correct
            },
        ]

        positions = service._aggregate_positions(components)
        aapl = next(p for p in positions if p["symbol"] == "AAPL")
        assert abs(aapl["profit_rate"] - 0.0526) < 0.01  # (1900-1800)/1800
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_portfolio_overview_service.py::TestAggregatePositions::test_us_mixed_source_uses_live_profit_rate -v -m unit`
Expected: FAIL — profit_rate is deeply negative due to mixed currencies

**Step 3: Fix `_aggregate_positions` to handle mixed-currency US positions**

Modify `app/services/portfolio_overview_service.py`, in the `_aggregate_positions` method (around line 833-885).

The fix: For US market positions with a live component, use the live component's `profit_rate` as the authoritative source, then back-derive `cost_basis` from the total `evaluation` and that rate.

```python
def _aggregate_positions(
    self, components: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}

    for item in components:
        key = (item["market_type"], item["symbol"])
        row = by_key.setdefault(
            key,
            {
                "market_type": item["market_type"],
                "symbol": item["symbol"],
                "name": item["name"],
                "components": [],
            },
        )

        if not row["name"] and item["name"]:
            row["name"] = item["name"]

        row["components"].append(
            {
                "account_key": item["account_key"],
                "broker": item["broker"],
                "account_name": item["account_name"],
                "source": item["source"],
                "quantity": item["quantity"],
                "avg_price": item["avg_price"],
                "current_price": item["current_price"],
                "evaluation": item["evaluation"],
                "profit_loss": item["profit_loss"],
                "profit_rate": item["profit_rate"],
            }
        )

    rows: list[dict[str, Any]] = []
    for row in by_key.values():
        components_list = row["components"]
        quantity = sum(_to_float(item.get("quantity")) for item in components_list)
        if quantity <= 0:
            continue

        avg_numerator = sum(
            _to_float(item.get("quantity")) * _to_float(item.get("avg_price"))
            for item in components_list
        )
        avg_price = avg_numerator / quantity if quantity > 0 else 0.0

        current_price = self._pick_current_price(components_list)

        # For multi-source US positions, use the live component's profit_rate
        # to avoid mixing KRW (manual) and USD (KIS) avg_prices in cost_basis.
        live_component = next(
            (c for c in components_list if c.get("source") == "live"),
            None,
        )
        is_mixed_us = (
            row["market_type"] == _MARKET_US
            and len(components_list) > 1
            and live_component is not None
        )

        if is_mixed_us and current_price is not None:
            # Use live component's profit_rate as authoritative
            live_rate = live_component.get("profit_rate")
            evaluation = quantity * current_price
            if live_rate is not None:
                denominator = 1.0 + float(live_rate)
                if denominator > 0:
                    cost_basis = evaluation / denominator
                else:
                    cost_basis = evaluation
                profit_loss = evaluation - cost_basis
                profit_rate = float(live_rate)
            else:
                cost_basis = sum(
                    _to_float(item.get("quantity")) * _to_float(item.get("avg_price"))
                    for item in components_list
                )
                profit_loss = evaluation - cost_basis
                profit_rate = (profit_loss / cost_basis) if cost_basis > 0 else 0.0
        elif current_price is not None:
            # Standard path: single-source or same-currency markets
            evaluation = quantity * current_price
            cost_basis = sum(
                _to_float(item.get("quantity")) * _to_float(item.get("avg_price"))
                for item in components_list
            )
            profit_loss = evaluation - cost_basis
            profit_rate = (profit_loss / cost_basis) if cost_basis > 0 else 0.0
        else:
            evaluation = sum(
                _to_float(item.get("evaluation"), default=0.0)
                for item in components_list
                if item.get("evaluation") is not None
            )
            profit_loss = sum(
                _to_float(item.get("profit_loss"), default=0.0)
                for item in components_list
                if item.get("profit_loss") is not None
            )
            cost_basis = sum(
                _to_float(item.get("quantity")) * _to_float(item.get("avg_price"))
                for item in components_list
            )
            profit_rate = (profit_loss / cost_basis) if cost_basis > 0 else 0.0

        rows.append(
            {
                "market_type": row["market_type"],
                "symbol": row["symbol"],
                "name": row["name"] or row["symbol"],
                "quantity": quantity,
                "avg_price": avg_price,
                "current_price": current_price,
                "evaluation": evaluation,
                "profit_loss": profit_loss,
                "profit_rate": profit_rate,
                "components": components_list,
            }
        )

    return sorted(
        rows,
        key=lambda item: (
            _MARKET_ORDER.get(item["market_type"], 999),
            item["symbol"],
        ),
    )
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_portfolio_overview_service.py -v -m unit`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add tests/test_portfolio_overview_service.py app/services/portfolio_overview_service.py
git commit -m "fix(portfolio): handle mixed-currency US position aggregation (#327)"
```

---

### Task 3: Run full test suite and lint

**Step 1: Run lint**

Run: `make lint`
Expected: PASS (no new lint issues)

**Step 2: Run full test suite**

Run: `make test`
Expected: PASS

**Step 3: Fix any issues found**

If lint or tests fail, fix minimally without refactoring unrelated code.

**Step 4: Commit fixes if needed**

```bash
git add -A
git commit -m "fix: address lint/test issues from #327 fix"
```

---

### Task 4: Verify end-to-end (manual)

**Step 1: Verify the fix works with realistic data**

Run the daily brief endpoint locally (if possible):
```bash
curl -s "http://localhost:8000/api/n8n/daily-brief" | python -m json.tool | grep -A5 '"us"'
```

Expected: US portfolio PnL should show a reasonable percentage (not -96.8%).

**Step 2: Verify the brief_text output is correct**

Check that the `brief_text` field formats the US portfolio line correctly:
```
[미국] $XX,XXX (+X.X%)
```
Not:
```
[미국] $47,349 (-96.8%)
```

---

## Summary of Changes

| File | Change |
|------|--------|
| `app/services/n8n_daily_brief_service.py` | `_build_portfolio_summary`: derive cost from `evaluation/(1+profit_rate)` instead of `avg_price*quantity` |
| `app/services/portfolio_overview_service.py` | `_aggregate_positions`: for mixed-source US positions, use live component's `profit_rate` as authoritative |
| `tests/test_n8n_daily_brief_service.py` | New unit tests for portfolio PnL calculation with mixed-currency scenarios |
| `tests/test_portfolio_overview_service.py` | New unit tests for mixed-source US position aggregation |

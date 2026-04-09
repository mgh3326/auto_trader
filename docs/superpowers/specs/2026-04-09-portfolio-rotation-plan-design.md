# Portfolio Rotation Plan — Design Spec

**Date:** 2026-04-09
**Scope:** Crypto / Upbit MVP first. Non-crypto markets return `supported: false`.

## Goal

Help the user reduce weak crypto positions and rotate into stronger candidates.
This is a **recommendation / planning layer**, not auto-trading logic.

**Key constraint:** No new MCP tool. Extend existing `analyze_portfolio` with an optional flag, share logic via a backend service, and surface results on the existing portfolio dashboard.

---

## 1. New Backend Service

### File: `app/services/portfolio_rotation_service.py`

**Class: `PortfolioRotationService`**

Dependencies:
- `AsyncSessionLocal` via `_session_factory()` (same pattern as `trade_journal_tools.py`) — self-manages DB sessions so it works from both MCP and router contexts without requiring session injection.

Collaborators (called internally):
- `_collect_portfolio_positions(...)` from `portfolio_holdings.py` — current positions + strategy signals (internal helper, not the MCP-decorated function)
- `TradeJournal` model query — active journals per position (direct SQLAlchemy, not via MCP tool)
- `screen_stocks_impl(market="crypto", strategy="oversold", ...)` — buy candidates

### Core Method

```python
class PortfolioRotationService:
    async def build_rotation_plan(
        self,
        *,
        market: str = "crypto",
        account: str | None = None,
    ) -> dict[str, Any]:
```

No constructor args needed — uses `_session_factory()` internally like `trade_journal_tools.py`.

#### Algorithm

1. **Guard:** If `market != "crypto"`, return `{"supported": False, "market": market, "warning": "Rotation plan is currently supported for crypto only."}`.

2. **Fetch holdings:** Call internal position collector for Upbit crypto positions with `include_current_price=True`, `minimum_value=None` (include dust). Reuse `_collect_portfolio_positions` or the public `get_holdings` helper from `portfolio_holdings.py` (import the underlying async helpers, not the MCP-decorated function).

3. **Fetch journals:** Query `TradeJournal` where `instrument_type = "crypto"` and `status in ("draft", "active")`. Build a `dict[symbol, journal_snapshot]`.

4. **Classify each position** into one of four buckets:

   | Bucket | Condition |
   |--------|-----------|
   | `locked_positions` | Journal strategy ∈ `LOCKED_STRATEGIES` (`coinmoogi_dca`, `staking_hold`, `index_dca`) **OR** `hold_until` not yet expired |
   | `ignored_positions` | Position evaluation < `DUST_THRESHOLD_KRW` (default 5,000 KRW) |
   | `sell_candidates` | Has `strategy_signal.action == "sell"` **OR** journal strategy == `dca_oversold` with `profit_rate < -3%` **OR** no journal + negative P&L |
   | (remaining) | Healthy positions — not surfaced |

   For each `sell_candidate`, attach:
   - `action`: `"reduce_partial"` (default) or `"reduce_full"` (only if stop-loss triggered)
   - `reduce_pct`: 30 for partial, 100 for full
   - `reason`: list of strings (from strategy_signal.reason, journal context, P&L)

5. **Fetch buy candidates:** Call `screen_stocks_impl(market="crypto", strategy="oversold", limit=10)`. Filter out symbols the user already holds. For each candidate, include `symbol`, `name`, `rsi` (if available in result), `trade_amount_24h`, `screen_reason`.

6. **Assemble response.**

### Constants

```python
LOCKED_STRATEGIES: set[str] = {"coinmoogi_dca", "staking_hold", "index_dca"}
DUST_THRESHOLD_KRW: float = 5_000
PARTIAL_REDUCE_PCT: int = 30
```

### Response Shape

```json
{
  "supported": true,
  "market": "crypto",
  "account": "upbit",
  "generated_at": "2026-04-09T14:30:00+09:00",
  "summary": {
    "total_positions": 12,
    "actionable_positions": 3,
    "locked_positions": 4,
    "ignored_positions": 2,
    "buy_candidates": 5
  },
  "sell_candidates": [
    {
      "symbol": "KRW-WLD",
      "name": "월드코인",
      "current_price": 1234,
      "profit_rate": -8.5,
      "evaluation_amount": 50000,
      "action": "reduce_partial",
      "reduce_pct": 30,
      "reason": ["stop_loss signal", "no active journal"],
      "journal_strategy": null
    }
  ],
  "buy_candidates": [
    {
      "symbol": "KRW-BARD",
      "name": "롬바드",
      "rsi": 28.5,
      "trade_amount_24h": 4700000000,
      "screen_reason": ["RSI oversold", "sufficient liquidity"]
    }
  ],
  "locked_positions": [
    {
      "symbol": "KRW-BTC",
      "name": "비트코인",
      "journal_strategy": "coinmoogi_dca",
      "lock_reason": "locked strategy"
    }
  ],
  "ignored_positions": [
    {
      "symbol": "KRW-SHIB",
      "name": "시바이누",
      "evaluation_amount": 1200,
      "ignore_reason": "dust position (< 5,000 KRW)"
    }
  ],
  "warnings": []
}
```

---

## 2. MCP Extension — `analyze_portfolio`

### File: `app/mcp_server/tooling/analysis_registration.py`

Add parameter to the registered tool:

```python
async def analyze_portfolio(
    symbols: list[str | int],
    market: str | None = None,
    include_peers: bool = False,
    include_rotation_plan: bool = False,   # NEW
) -> dict[str, Any]:
```

### File: `app/mcp_server/tooling/analysis_tool_handlers.py`

Add to `analyze_portfolio_impl`:

```python
async def analyze_portfolio_impl(
    symbols: list[str | int],
    market: str | None = None,
    include_peers: bool = False,
    include_rotation_plan: bool = False,   # NEW
) -> dict[str, Any]:
    result = await _run_batch_analysis(...)

    if include_rotation_plan:
        from app.services.portfolio_rotation_service import PortfolioRotationService
        rotation_service = PortfolioRotationService()
        rotation_plan = await rotation_service.build_rotation_plan(
            market=market or "crypto",
        )
        result["rotation_plan"] = rotation_plan

    return result
```

**Backward compatibility:** When `include_rotation_plan` is omitted or `False`, behavior is identical to today. The `rotation_plan` key simply doesn't appear in the response.

---

## 3. Portfolio API Endpoint

### File: `app/routers/portfolio.py`

```python
@router.get("/api/rotation-plan")
async def get_rotation_plan(
    _current_user: User = Depends(get_authenticated_user),
) -> dict[str, Any]:
    from app.services.portfolio_rotation_service import PortfolioRotationService
    service = PortfolioRotationService()
    return await service.build_rotation_plan(market="crypto")
```

Thin router — all logic in service.

---

## 4. Dashboard UI Panel

### File: `app/templates/portfolio_dashboard.html`

#### HTML — add after `portfolio-status-panel` (after line 683)

```html
<article class="panel" id="portfolio-rotation-panel" style="grid-column: 1 / -1;">
    <div class="d-flex justify-content-between align-items-center">
        <h2><i class="bi bi-arrow-repeat"></i> 로테이션 제안</h2>
        <div>
            <span class="subtle" id="rotation-summary">-</span>
            <button class="btn btn-sm btn-outline-secondary ms-2"
                    id="rotation-refresh-btn" title="새로고침">
                <i class="bi bi-arrow-clockwise"></i>
            </button>
        </div>
    </div>
    <div id="rotation-content" class="mt-2">
        <div class="subtle">CRYPTO 마켓을 선택하면 로테이션 제안을 확인할 수 있습니다.</div>
    </div>
</article>
```

The panel spans the full width (`grid-column: 1 / -1`) because it contains a small table/list layout that benefits from horizontal space.

#### JS — add to state and render

```javascript
// In state object
state.rotationPlan = null;

// Fetch function
async function fetchRotationPlan() { ... }

// Render function
function renderRotationPlan(plan) { ... }
```

**Trigger:** Fetch rotation plan after overview loads, only when market filter is `"ALL"` or `"CRYPTO"`. Show loading skeleton while fetching.

**Render sections:**
- **Sell Candidates:** Red-tinted rows with symbol, P&L, action badge (`부분 축소 30%`), reason chips
- **Buy Candidates:** Green-tinted rows with symbol, RSI badge, 24h volume
- **Locked:** Muted rows with lock icon and strategy name
- **Ignored:** Collapsed by default, expandable

---

## 5. Files Changed Summary

| File | Change |
|------|--------|
| `app/services/portfolio_rotation_service.py` | **NEW** — core rotation logic |
| `app/mcp_server/tooling/analysis_registration.py` | Add `include_rotation_plan` param |
| `app/mcp_server/tooling/analysis_tool_handlers.py` | Add rotation plan to `analyze_portfolio_impl` |
| `app/routers/portfolio.py` | Add `GET /api/rotation-plan` endpoint |
| `app/templates/portfolio_dashboard.html` | Add rotation panel HTML + JS |
| `tests/test_portfolio_rotation_service.py` | **NEW** — service unit tests |

---

## 6. Testing

### `tests/test_portfolio_rotation_service.py`

1. **test_unsupported_market** — `market="kr"` returns `supported: False`
2. **test_empty_portfolio** — no positions → empty buckets, only buy_candidates populated
3. **test_locked_strategy_classification** — positions with `coinmoogi_dca` strategy → `locked_positions`
4. **test_dust_position_ignored** — evaluation < 5,000 KRW → `ignored_positions`
5. **test_sell_candidate_from_stop_loss** — position with stop_loss signal → `sell_candidates` with `reduce_full`
6. **test_sell_candidate_partial_reduce** — `dca_oversold` strategy with loss → `sell_candidates` with `reduce_partial`
7. **test_buy_candidates_exclude_held** — screener results filtered to exclude existing holdings
8. **test_response_shape** — validate all required keys present

### Existing MCP tests extension

9. **test_analyze_portfolio_without_rotation** — existing behavior unchanged when flag omitted
10. **test_analyze_portfolio_with_rotation** — `include_rotation_plan=True` adds `rotation_plan` key

### Mocking strategy

- Mock `_collect_portfolio_positions` / `get_holdings` to return fixture positions
- Mock `get_trade_journal` to return fixture journals
- Mock `screen_stocks_impl` to return fixture screener results
- No real API calls in unit tests

---

## 7. Limitations / Follow-up

- **Crypto-only MVP:** KR/US markets return `supported: false`
- **No AI reasoning:** Classification is rule-based (strategy signals + journal strategy + P&L). AI-based reasoning could be a v2 enhancement.
- **No order execution:** This is recommendation only. The user must act manually.
- **Strategy field is free-form:** The locked strategies set is hardcoded. If the user adds new strategy names, the set needs updating.
- **Screener dependency:** Buy candidates depend on `screen_stocks_impl` which uses TradingView data. If TradingView is down, buy_candidates will be empty.

# ROB-582 Cross-Asset Allocation Roll-Up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only MCP tool that reports whole-portfolio allocation across KIS, Toss, Samsung/manual, Upbit, cash, and KR ETF look-through so KR/US/crypto/cash allocation decisions are not based on surface account labels.

**Architecture:** Keep broker reads in the existing MCP portfolio tooling and add a pure service that only receives normalized position/cash rows and returns KRW-based allocation roll-ups. ETF look-through is an additive classification layer using KRX ETF metadata when available and fail-open behavior when metadata is unavailable. No DB migration, no order execution path, and no changes to live order approval boundaries.

**Tech Stack:** Python 3.13, FastMCP, pytest, existing `app.mcp_server.tooling.portfolio_*`, `app.services.krx`, `app.services.exchange_rate_service`.

---

## Owner Decisions

1. **Tool surface:** default plan adds a new MCP tool named `get_portfolio_allocation`. It does not extend `analyze_portfolio`, because ROB-582 calls out that `analyze_portfolio` is a symbol analyzer, not an allocation roll-up.
2. **Target weights:** default plan does not hardcode recommended target weights. It supports optional `target_weights={"us_equity": 50, "kr_equity": 20, "crypto": 25, "cash": 5}` and emits over/under flags only when targets are provided.
3. **Non-US foreign or thematic KR ETFs:** default plan creates an `other` bucket for ETF exposures that are neither US nor KR equity, such as India, Japan, China, bonds, gold, oil, or unclear thematic ETFs. This avoids mislabeling them as Korean equity.
4. **Cash:** default plan includes cash in the allocation by default through `include_cash=True`.

## File Structure

- Create: `app/services/portfolio_allocation_service.py`
  - Pure allocation math, currency conversion, account/asset-class roll-ups, target drift flags, and ETF exposure classification from supplied ETF metadata.
- Create: `app/mcp_server/tooling/portfolio_allocation.py`
  - MCP handler for `get_portfolio_allocation`; calls existing holdings/cash collectors, exchange-rate service, KRX ETF metadata, then delegates to the pure service.
- Modify: `app/mcp_server/tooling/portfolio_registration.py`
  - Register the new allocation tool alongside existing portfolio tools and export the combined tool-name set.
- Modify: `tests/_mcp_tooling_support.py`
  - Add `portfolio_allocation` to patchable MCP modules for tests.
- Create: `tests/services/test_portfolio_allocation_service.py`
  - Unit coverage for KRW conversion, ETF look-through, cash inclusion, target drift flags, and fail-open ETF metadata behavior.
- Create: `tests/test_mcp_portfolio_allocation.py`
  - MCP contract tests for registration, account routing, cash toggle, and error propagation.
- Modify: `tests/test_mcp_tool_registration.py`
  - Assert `get_portfolio_allocation` is present in the default MCP tool surface.
- Modify: `app/mcp_server/README.md`
  - Document parameters, response shape, look-through semantics, and limitations.

## Response Contract

The MCP tool returns this stable shape:

```python
{
    "filters": {
        "account": "kis",
        "market": None,
        "include_cash": True,
        "include_positions": False,
        "target_weights": {"us_equity": 50.0, "kr_equity": 20.0},
    },
    "currency": {
        "base": "KRW",
        "usd_krw": 1400.0,
    },
    "summary": {
        "total_value_krw": 156000000.0,
        "invested_value_krw": 150000000.0,
        "cash_value_krw": 6000000.0,
        "valued_position_count": 12,
        "unvalued_position_count": 0,
    },
    "asset_classes": [
        {
            "asset_class": "us_equity",
            "label": "미국주식",
            "value_krw": 98000000.0,
            "weight_pct": 62.8,
            "direct_value_krw": 80000000.0,
            "lookthrough_value_krw": 18000000.0,
            "cash_value_krw": 0.0,
            "target_weight_pct": 50.0,
            "drift_pct": 12.8,
            "weight_status": "overweight",
        }
    ],
    "accounts": [
        {
            "account": "kis",
            "broker": "kis",
            "account_name": "기본 계좌",
            "value_krw": 98000000.0,
            "weight_pct": 62.8,
            "asset_classes": [
                {"asset_class": "us_equity", "value_krw": 80000000.0, "weight_pct": 51.3}
            ],
        }
    ],
    "lookthrough": [
        {
            "symbol": "360750",
            "name": "TIGER 미국S&P500",
            "account": "kis",
            "surface_asset_class": "kr_equity",
            "effective_asset_class": "us_equity",
            "value_krw": 10000000.0,
            "rule": "kr_etf_category:미국주식",
        }
    ],
    "positions": [],
    "cash": [],
    "errors": [],
    "warnings": [],
}
```

Asset class keys:

```python
ASSET_CLASS_LABELS = {
    "us_equity": "미국주식",
    "kr_equity": "한국주식",
    "crypto": "코인",
    "cash": "현금",
    "other": "기타",
}
```

## Task 1: Pure Allocation Service

**Files:**
- Create: `app/services/portfolio_allocation_service.py`
- Test: `tests/services/test_portfolio_allocation_service.py`

- [ ] **Step 1: Write failing tests for base roll-up and KR ETF look-through**

Add `tests/services/test_portfolio_allocation_service.py`:

```python
import pytest

from app.services.portfolio_allocation_service import build_portfolio_allocation


def test_build_allocation_converts_usd_and_looks_through_kr_us_etf() -> None:
    positions = [
        {
            "account": "kis",
            "account_name": "기본 계좌",
            "broker": "kis",
            "instrument_type": "equity_us",
            "market": "us",
            "symbol": "AAPL",
            "name": "Apple",
            "evaluation_amount": 1000.0,
            "profit_loss": 100.0,
        },
        {
            "account": "kis",
            "account_name": "기본 계좌",
            "broker": "kis",
            "instrument_type": "equity_kr",
            "market": "kr",
            "symbol": "360750",
            "name": "TIGER 미국S&P500",
            "evaluation_amount": 700000.0,
            "profit_loss": 70000.0,
        },
        {
            "account": "upbit",
            "account_name": "기본 계좌",
            "broker": "upbit",
            "instrument_type": "crypto",
            "market": "crypto",
            "symbol": "KRW-BTC",
            "name": "비트코인",
            "evaluation_amount": 300000.0,
            "profit_loss": -30000.0,
        },
    ]
    cash_accounts = [
        {
            "account": "kis_domestic",
            "account_name": "기본 계좌",
            "broker": "kis",
            "currency": "KRW",
            "balance": 100000.0,
        },
        {
            "account": "kis_overseas",
            "account_name": "기본 계좌",
            "broker": "kis",
            "currency": "USD",
            "balance": 100.0,
        },
    ]
    etf_rows = [
        {
            "short_code": "360750",
            "code": "KR7360750004",
            "name": "TIGER 미국S&P500",
            "index_name": "S&P 500",
        }
    ]

    result = build_portfolio_allocation(
        positions=positions,
        cash_accounts=cash_accounts,
        usd_krw=1400.0,
        etf_rows=etf_rows,
        include_cash=True,
        include_positions=False,
        target_weights={"us_equity": 50.0, "crypto": 25.0},
        drift_threshold_pct=5.0,
    )

    assert result["summary"]["total_value_krw"] == pytest.approx(2540000.0)
    by_class = {row["asset_class"]: row for row in result["asset_classes"]}
    assert by_class["us_equity"]["value_krw"] == pytest.approx(2100000.0)
    assert by_class["us_equity"]["direct_value_krw"] == pytest.approx(1400000.0)
    assert by_class["us_equity"]["lookthrough_value_krw"] == pytest.approx(700000.0)
    assert by_class["crypto"]["value_krw"] == pytest.approx(300000.0)
    assert by_class["cash"]["value_krw"] == pytest.approx(240000.0)
    assert by_class["us_equity"]["weight_status"] == "overweight"
    assert result["lookthrough"][0]["effective_asset_class"] == "us_equity"
    assert result["positions"] == []
```

- [ ] **Step 2: Run the new service test to verify it fails**

Run:

```bash
uv run pytest --no-cov tests/services/test_portfolio_allocation_service.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.portfolio_allocation_service'`.

- [ ] **Step 3: Add the pure service implementation**

Create `app/services/portfolio_allocation_service.py` with these public functions:

```python
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.services.krx import classify_etf_category

ASSET_CLASS_LABELS = {
    "us_equity": "미국주식",
    "kr_equity": "한국주식",
    "crypto": "코인",
    "cash": "현금",
    "other": "기타",
}

_KR_ETF_OTHER_CATEGORIES = {
    "인도",
    "일본",
    "중국",
    "채권",
    "금",
    "원유",
}


def _to_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _round_money(value: float) -> float:
    return round(value, 2)


def _round_pct(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _build_etf_lookup(etf_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for row in etf_rows:
        for key in ("short_code", "code"):
            symbol = _normalize_symbol(row.get(key))
            if symbol:
                lookup[symbol] = row
    return lookup


def _position_value_krw(position: dict[str, Any], usd_krw: float) -> float | None:
    value = position.get("evaluation_amount")
    if value is None:
        return None
    amount = _to_float(value)
    if amount <= 0:
        return None
    if position.get("instrument_type") == "equity_us":
        return amount * usd_krw
    return amount


def _profit_loss_krw(position: dict[str, Any], usd_krw: float) -> float | None:
    value = position.get("profit_loss")
    if value is None:
        return None
    amount = _to_float(value)
    if position.get("instrument_type") == "equity_us":
        return amount * usd_krw
    return amount


def _surface_asset_class(position: dict[str, Any]) -> str:
    instrument_type = str(position.get("instrument_type") or "")
    if instrument_type == "equity_us":
        return "us_equity"
    if instrument_type == "equity_kr":
        return "kr_equity"
    if instrument_type == "crypto":
        return "crypto"
    return "other"


def _effective_asset_class(
    position: dict[str, Any],
    etf_lookup: dict[str, dict[str, Any]],
) -> tuple[str, str | None, dict[str, Any] | None]:
    surface = _surface_asset_class(position)
    if surface != "kr_equity":
        return surface, None, None

    symbol = _normalize_symbol(position.get("symbol"))
    etf = etf_lookup.get(symbol)
    if etf is None:
        return surface, None, None

    categories = classify_etf_category(
        str(etf.get("name") or position.get("name") or ""),
        str(etf.get("index_name") or ""),
    )
    if "미국주식" in categories:
        return "us_equity", "kr_etf_category:미국주식", etf
    if "코스피200" in categories or "코스닥150" in categories:
        return "kr_equity", "kr_etf_category:한국지수", etf
    if any(category in _KR_ETF_OTHER_CATEGORIES for category in categories):
        return "other", "kr_etf_category:" + ",".join(categories), etf
    return "kr_equity", "kr_etf_category:" + ",".join(categories), etf


def _weight_status(
    *,
    weight_pct: float,
    target_pct: float | None,
    drift_threshold_pct: float,
) -> tuple[float | None, str | None]:
    if target_pct is None:
        return None, None
    drift = round(weight_pct - target_pct, 2)
    if drift >= drift_threshold_pct:
        return drift, "overweight"
    if drift <= -drift_threshold_pct:
        return drift, "underweight"
    return drift, "neutral"


def build_portfolio_allocation(
    *,
    positions: list[dict[str, Any]],
    cash_accounts: list[dict[str, Any]],
    usd_krw: float,
    etf_rows: list[dict[str, Any]],
    include_cash: bool,
    include_positions: bool,
    target_weights: dict[str, float] | None = None,
    drift_threshold_pct: float = 5.0,
) -> dict[str, Any]:
    target_weights = target_weights or {}
    etf_lookup = _build_etf_lookup(etf_rows)
    warnings: list[dict[str, Any]] = []
    lookthrough: list[dict[str, Any]] = []
    class_totals: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "value_krw": 0.0,
            "direct_value_krw": 0.0,
            "lookthrough_value_krw": 0.0,
            "cash_value_krw": 0.0,
            "profit_loss_krw": 0.0,
        }
    )
    account_totals: dict[str, dict[str, Any]] = {}
    valued_position_count = 0
    unvalued_position_count = 0
    output_positions: list[dict[str, Any]] = []

    for position in positions:
        value_krw = _position_value_krw(position, usd_krw)
        if value_krw is None:
            unvalued_position_count += 1
            warnings.append(
                {
                    "source": "allocation",
                    "symbol": position.get("symbol"),
                    "reason": "position_value_unavailable",
                }
            )
            continue

        valued_position_count += 1
        surface_class = _surface_asset_class(position)
        effective_class, rule, etf = _effective_asset_class(position, etf_lookup)
        profit_loss = _profit_loss_krw(position, usd_krw)
        totals = class_totals[effective_class]
        totals["value_krw"] += value_krw
        if surface_class == effective_class:
            totals["direct_value_krw"] += value_krw
        else:
            totals["lookthrough_value_krw"] += value_krw
        if profit_loss is not None:
            totals["profit_loss_krw"] += profit_loss

        account_id = str(position.get("account") or "unknown")
        account = account_totals.setdefault(
            account_id,
            {
                "account": account_id,
                "broker": position.get("broker"),
                "account_name": position.get("account_name"),
                "value_krw": 0.0,
                "asset_classes": defaultdict(float),
            },
        )
        account["value_krw"] += value_krw
        account["asset_classes"][effective_class] += value_krw

        if surface_class != effective_class:
            lookthrough.append(
                {
                    "symbol": position.get("symbol"),
                    "name": position.get("name") or (etf or {}).get("name"),
                    "account": account_id,
                    "surface_asset_class": surface_class,
                    "effective_asset_class": effective_class,
                    "value_krw": _round_money(value_krw),
                    "rule": rule,
                }
            )

        if include_positions:
            row = dict(position)
            row["surface_asset_class"] = surface_class
            row["effective_asset_class"] = effective_class
            row["value_krw"] = _round_money(value_krw)
            output_positions.append(row)

    cash_rows: list[dict[str, Any]] = []
    if include_cash:
        for cash in cash_accounts:
            currency = str(cash.get("currency") or "KRW").upper()
            balance = _to_float(cash.get("balance"))
            value_krw = balance * usd_krw if currency == "USD" else balance
            if value_krw <= 0:
                continue
            totals = class_totals["cash"]
            totals["value_krw"] += value_krw
            totals["cash_value_krw"] += value_krw
            account_id = str(cash.get("account") or "cash")
            account = account_totals.setdefault(
                account_id,
                {
                    "account": account_id,
                    "broker": cash.get("broker"),
                    "account_name": cash.get("account_name"),
                    "value_krw": 0.0,
                    "asset_classes": defaultdict(float),
                },
            )
            account["value_krw"] += value_krw
            account["asset_classes"]["cash"] += value_krw
            cash_rows.append({**cash, "value_krw": _round_money(value_krw)})

    total_value = sum(row["value_krw"] for row in class_totals.values())
    invested_value = total_value - class_totals["cash"]["value_krw"]
    asset_classes = []
    for asset_class, totals in sorted(class_totals.items()):
        value = totals["value_krw"]
        if value <= 0:
            continue
        weight = (value / total_value) * 100 if total_value else 0.0
        target = target_weights.get(asset_class)
        drift, status = _weight_status(
            weight_pct=weight,
            target_pct=target,
            drift_threshold_pct=drift_threshold_pct,
        )
        asset_classes.append(
            {
                "asset_class": asset_class,
                "label": ASSET_CLASS_LABELS[asset_class],
                "value_krw": _round_money(value),
                "weight_pct": _round_pct(weight),
                "direct_value_krw": _round_money(totals["direct_value_krw"]),
                "lookthrough_value_krw": _round_money(totals["lookthrough_value_krw"]),
                "cash_value_krw": _round_money(totals["cash_value_krw"]),
                "profit_loss_krw": _round_money(totals["profit_loss_krw"]),
                "target_weight_pct": target,
                "drift_pct": drift,
                "weight_status": status,
            }
        )
    asset_classes.sort(key=lambda row: row["value_krw"], reverse=True)

    accounts = []
    for account in account_totals.values():
        account_value = account["value_krw"]
        accounts.append(
            {
                "account": account["account"],
                "broker": account["broker"],
                "account_name": account["account_name"],
                "value_krw": _round_money(account_value),
                "weight_pct": _round_pct(
                    (account_value / total_value) * 100 if total_value else 0.0
                ),
                "asset_classes": [
                    {
                        "asset_class": key,
                        "value_krw": _round_money(value),
                        "weight_pct": _round_pct(
                            (value / total_value) * 100 if total_value else 0.0
                        ),
                    }
                    for key, value in sorted(account["asset_classes"].items())
                    if value > 0
                ],
            }
        )
    accounts.sort(key=lambda row: row["value_krw"], reverse=True)

    return {
        "currency": {"base": "KRW", "usd_krw": usd_krw},
        "summary": {
            "total_value_krw": _round_money(total_value),
            "invested_value_krw": _round_money(invested_value),
            "cash_value_krw": _round_money(class_totals["cash"]["value_krw"]),
            "valued_position_count": valued_position_count,
            "unvalued_position_count": unvalued_position_count,
        },
        "asset_classes": asset_classes,
        "accounts": accounts,
        "lookthrough": lookthrough,
        "positions": output_positions,
        "cash": cash_rows if include_cash else [],
        "warnings": warnings,
    }
```

- [ ] **Step 4: Run service tests**

Run:

```bash
uv run pytest --no-cov tests/services/test_portfolio_allocation_service.py -q
```

Expected: PASS.

- [ ] **Step 5: Add fail-open and target flag service tests**

Append tests for non-US ETF and missing valuation:

```python
def test_build_allocation_puts_non_us_foreign_etf_in_other_bucket() -> None:
    positions = [
        {
            "account": "kis",
            "account_name": "기본 계좌",
            "broker": "kis",
            "instrument_type": "equity_kr",
            "market": "kr",
            "symbol": "453870",
            "name": "TIGER 인도니프티50",
            "evaluation_amount": 500000.0,
        }
    ]
    result = build_portfolio_allocation(
        positions=positions,
        cash_accounts=[],
        usd_krw=1400.0,
        etf_rows=[
            {
                "short_code": "453870",
                "name": "TIGER 인도니프티50",
                "index_name": "Nifty 50",
            }
        ],
        include_cash=False,
        include_positions=True,
    )

    by_class = {row["asset_class"]: row for row in result["asset_classes"]}
    assert by_class["other"]["value_krw"] == pytest.approx(500000.0)
    assert result["lookthrough"][0]["effective_asset_class"] == "other"


def test_build_allocation_warns_and_skips_unvalued_positions() -> None:
    result = build_portfolio_allocation(
        positions=[
            {
                "account": "kis",
                "account_name": "기본 계좌",
                "broker": "kis",
                "instrument_type": "equity_kr",
                "market": "kr",
                "symbol": "005930",
                "name": "삼성전자",
                "evaluation_amount": None,
            }
        ],
        cash_accounts=[],
        usd_krw=1400.0,
        etf_rows=[],
        include_cash=False,
        include_positions=False,
    )

    assert result["summary"]["total_value_krw"] == pytest.approx(0.0)
    assert result["summary"]["unvalued_position_count"] == 1
    assert result["warnings"][0]["reason"] == "position_value_unavailable"
```

- [ ] **Step 6: Run service tests again**

Run:

```bash
uv run pytest --no-cov tests/services/test_portfolio_allocation_service.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit service slice**

Run:

```bash
git add app/services/portfolio_allocation_service.py tests/services/test_portfolio_allocation_service.py
git commit -m "feat: add cross-asset allocation rollup service"
```

## Task 2: MCP Tool Handler

**Files:**
- Create: `app/mcp_server/tooling/portfolio_allocation.py`
- Modify: `tests/_mcp_tooling_support.py`
- Test: `tests/test_mcp_portfolio_allocation.py`

- [ ] **Step 1: Write failing MCP handler tests**

Add `tests/test_mcp_portfolio_allocation.py`:

```python
import pytest

from app.mcp_server.tooling import portfolio_allocation
from tests._mcp_tooling_support import DummyMCP


@pytest.mark.asyncio
async def test_get_portfolio_allocation_handler_rolls_up_positions_cash_and_errors(
    monkeypatch,
) -> None:
    async def fake_collect_positions(**kwargs):
        assert kwargs["account"] is None
        assert kwargs["market"] is None
        assert kwargs["include_current_price"] is True
        return (
            [
                {
                    "account": "kis",
                    "account_name": "기본 계좌",
                    "broker": "kis",
                    "instrument_type": "equity_us",
                    "market": "us",
                    "symbol": "AAPL",
                    "name": "Apple",
                    "evaluation_amount": 1000.0,
                }
            ],
            [{"source": "holdings", "error": "partial"}],
            None,
            None,
        )

    async def fake_cash_balance(**kwargs):
        assert kwargs["account"] is None
        return {
            "accounts": [
                {
                    "account": "upbit",
                    "account_name": "기본 계좌",
                    "broker": "upbit",
                    "currency": "KRW",
                    "balance": 200000.0,
                }
            ],
            "errors": [{"source": "cash", "error": "partial"}],
        }

    monkeypatch.setattr(portfolio_allocation, "_collect_portfolio_positions", fake_collect_positions)
    monkeypatch.setattr(portfolio_allocation, "get_cash_balance_impl", fake_cash_balance)
    monkeypatch.setattr(portfolio_allocation, "get_usd_krw_rate", lambda: 1400.0)
    monkeypatch.setattr(portfolio_allocation, "fetch_etf_all_cached", lambda: [])

    result = await portfolio_allocation.get_portfolio_allocation_impl(
        account=None,
        market=None,
        include_cash=True,
        include_positions=False,
        target_weights=None,
        drift_threshold_pct=5.0,
        is_mock=False,
    )

    assert result["summary"]["total_value_krw"] == pytest.approx(1600000.0)
    assert result["errors"] == [
        {"source": "holdings", "error": "partial"},
        {"source": "cash", "error": "partial"},
    ]


@pytest.mark.asyncio
async def test_get_portfolio_allocation_tool_is_registered(monkeypatch) -> None:
    async def fake_impl(**kwargs):
        assert kwargs["include_cash"] is True
        return {"ok": True}

    monkeypatch.setattr(portfolio_allocation, "get_portfolio_allocation_impl", fake_impl)
    mcp = DummyMCP()
    portfolio_allocation.register_portfolio_allocation_tool(mcp)

    result = await mcp.tools["get_portfolio_allocation"]()

    assert result == {"ok": True}
```

- [ ] **Step 2: Run MCP allocation tests to verify they fail**

Run:

```bash
uv run pytest --no-cov tests/test_mcp_portfolio_allocation.py -q
```

Expected: FAIL with missing `app.mcp_server.tooling.portfolio_allocation`.

- [ ] **Step 3: Add the MCP allocation module**

Create `app/mcp_server/tooling/portfolio_allocation.py`:

```python
"""Cross-asset portfolio allocation MCP tool."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.config import validate_kis_mock_config
from app.mcp_server.tooling.account_modes import (
    apply_account_routing_metadata,
    normalize_account_mode,
)
from app.mcp_server.tooling.portfolio_cash import (
    get_cash_balance_impl,
    get_usd_krw_rate,
)
from app.mcp_server.tooling.portfolio_holdings import _collect_portfolio_positions
from app.services.krx import fetch_etf_all_cached
from app.services.portfolio_allocation_service import build_portfolio_allocation

if TYPE_CHECKING:
    from fastmcp import FastMCP


ALLOCATION_TOOL_NAMES: set[str] = {"get_portfolio_allocation"}


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


async def _load_etf_rows(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        rows = await _maybe_await(fetch_etf_all_cached())
    except Exception as exc:
        errors.append({"source": "krx_etf", "error": str(exc), "degraded": True})
        return []
    return rows if isinstance(rows, list) else []


async def get_portfolio_allocation_impl(
    *,
    account: str | None = None,
    market: str | None = None,
    include_cash: bool = True,
    include_positions: bool = False,
    target_weights: dict[str, float] | None = None,
    drift_threshold_pct: float = 5.0,
    is_mock: bool = False,
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    positions, position_errors, resolved_market, resolved_account = (
        await _collect_portfolio_positions(
            account=account,
            market=market,
            include_current_price=True,
            is_mock=is_mock,
        )
    )
    errors.extend(position_errors)

    cash_accounts: list[dict[str, Any]] = []
    if include_cash:
        cash_result = await get_cash_balance_impl(account=account, is_mock=is_mock)
        cash_accounts = list(cash_result.get("accounts", []))
        errors.extend(cash_result.get("errors", []))

    usd_krw = await _maybe_await(get_usd_krw_rate())
    etf_rows = await _load_etf_rows(errors)
    result = build_portfolio_allocation(
        positions=positions,
        cash_accounts=cash_accounts,
        usd_krw=float(usd_krw),
        etf_rows=etf_rows,
        include_cash=include_cash,
        include_positions=include_positions,
        target_weights=target_weights,
        drift_threshold_pct=drift_threshold_pct,
    )
    result["filters"] = {
        "account": resolved_account,
        "market": resolved_market,
        "include_cash": include_cash,
        "include_positions": include_positions,
        "target_weights": target_weights,
        "drift_threshold_pct": drift_threshold_pct,
    }
    result["errors"] = errors
    return result


def register_portfolio_allocation_tool(mcp: FastMCP) -> None:
    @mcp.tool(
        name="get_portfolio_allocation",
        description=(
            "Read-only cross-asset allocation roll-up across holdings and cash. "
            "Converts USD holdings/cash to KRW, classifies US/KR/crypto/cash, "
            "and looks through KR-listed ETFs such as TIGER/KODEX/SOL/RISE "
            "US index ETFs into effective US equity exposure. No order actions "
            "are performed. target_weights is optional and only controls "
            "overweight/underweight flags."
        ),
    )
    async def get_portfolio_allocation(
        account: str | None = None,
        market: str | None = None,
        include_cash: bool = True,
        include_positions: bool = False,
        target_weights: dict[str, float] | None = None,
        drift_threshold_pct: float = 5.0,
        account_mode: str | None = None,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        routing = normalize_account_mode(
            account_mode=account_mode,
            account_type=account_type,
        )
        if routing.is_db_simulated and account is None:
            account = "paper"
        if routing.is_kis_mock:
            missing = validate_kis_mock_config()
            if missing:
                raise RuntimeError(
                    "KIS mock account is disabled or missing required "
                    "configuration: " + ", ".join(missing)
                )
        return apply_account_routing_metadata(
            await get_portfolio_allocation_impl(
                account=account,
                market=market,
                include_cash=include_cash,
                include_positions=include_positions,
                target_weights=target_weights,
                drift_threshold_pct=drift_threshold_pct,
                is_mock=routing.is_kis_mock,
            ),
            routing,
        )


__all__ = [
    "ALLOCATION_TOOL_NAMES",
    "get_portfolio_allocation_impl",
    "register_portfolio_allocation_tool",
]
```

- [ ] **Step 4: Add module to test patch support**

Modify `tests/_mcp_tooling_support.py` imports:

```python
from app.mcp_server.tooling import (
    analysis_analyze,
    analysis_rankings,
    analysis_recommend,
    analysis_screen_core,
    analysis_screening,
    analysis_tool_handlers,
    fundamentals_handlers,
    fundamentals_sources_binance,
    fundamentals_sources_coingecko,
    fundamentals_sources_common,
    fundamentals_sources_finnhub,
    fundamentals_sources_indices,
    fundamentals_sources_naver,
    fundamentals_sources_yfinance,
    market_data_indicators,
    market_data_quotes,
    order_execution,
    order_journal,
    order_validation,
    orders_history,
    orders_modify_cancel,
    portfolio_allocation,
    portfolio_cash,
    portfolio_holdings,
    trade_journal_tools,
)
```

Add `portfolio_allocation` to `_PATCH_MODULES` next to `portfolio_cash` and `portfolio_holdings`.

- [ ] **Step 5: Run MCP allocation tests**

Run:

```bash
uv run pytest --no-cov tests/test_mcp_portfolio_allocation.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit MCP handler slice**

Run:

```bash
git add app/mcp_server/tooling/portfolio_allocation.py tests/_mcp_tooling_support.py tests/test_mcp_portfolio_allocation.py
git commit -m "feat: add portfolio allocation MCP handler"
```

## Task 3: Tool Registration

**Files:**
- Modify: `app/mcp_server/tooling/portfolio_registration.py`
- Modify: `tests/test_mcp_tool_registration.py`

- [ ] **Step 1: Add failing registration assertion**

Append to `tests/test_mcp_tool_registration.py`:

```python
@pytest.mark.asyncio
async def test_get_portfolio_allocation_registered_in_default_surface() -> None:
    tools = build_tools()

    assert "get_portfolio_allocation" in tools
```

- [ ] **Step 2: Run registration test to verify it fails**

Run:

```bash
uv run pytest --no-cov tests/test_mcp_tool_registration.py -k "portfolio_allocation" -q
```

Expected: FAIL because the new tool is not registered by `register_all_tools()`.

- [ ] **Step 3: Register the allocation tool**

Modify `app/mcp_server/tooling/portfolio_registration.py`:

```python
"""Portfolio MCP tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.portfolio_allocation import (
    ALLOCATION_TOOL_NAMES,
    register_portfolio_allocation_tool,
)
from app.mcp_server.tooling.portfolio_holdings import (
    PORTFOLIO_TOOL_NAMES as HOLDINGS_TOOL_NAMES,
)
from app.mcp_server.tooling.portfolio_holdings import (
    _register_portfolio_tools_impl,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

PORTFOLIO_TOOL_NAMES: set[str] = HOLDINGS_TOOL_NAMES | ALLOCATION_TOOL_NAMES


def register_portfolio_tools(mcp: FastMCP) -> None:
    _register_portfolio_tools_impl(mcp)
    register_portfolio_allocation_tool(mcp)


__all__ = ["PORTFOLIO_TOOL_NAMES", "register_portfolio_tools"]
```

- [ ] **Step 4: Run registration tests**

Run:

```bash
uv run pytest --no-cov tests/test_mcp_tool_registration.py -k "portfolio_allocation or rob488" -q
```

Expected: PASS.

- [ ] **Step 5: Commit registration slice**

Run:

```bash
git add app/mcp_server/tooling/portfolio_registration.py tests/test_mcp_tool_registration.py
git commit -m "feat: register portfolio allocation MCP tool"
```

## Task 4: Routing and Edge-Case Tests

**Files:**
- Modify: `tests/test_mcp_portfolio_allocation.py`

- [ ] **Step 1: Add KIS mock routing test**

Append:

```python
@pytest.mark.asyncio
async def test_get_portfolio_allocation_tool_passes_kis_mock(monkeypatch) -> None:
    calls = []

    async def fake_impl(**kwargs):
        calls.append(kwargs)
        return {"summary": {"total_value_krw": 0.0}}

    monkeypatch.setattr(portfolio_allocation, "get_portfolio_allocation_impl", fake_impl)
    monkeypatch.setattr(portfolio_allocation, "validate_kis_mock_config", lambda: [])
    mcp = DummyMCP()
    portfolio_allocation.register_portfolio_allocation_tool(mcp)

    result = await mcp.tools["get_portfolio_allocation"](account_mode="kis_mock")

    assert result["account_mode"] == "kis_mock"
    assert calls[0]["is_mock"] is True
```

- [ ] **Step 2: Add cash toggle test**

Append:

```python
@pytest.mark.asyncio
async def test_get_portfolio_allocation_can_exclude_cash(monkeypatch) -> None:
    async def fake_collect_positions(**kwargs):
        return ([], [], None, None)

    async def fail_cash_balance(**kwargs):
        raise AssertionError("cash balance must not be queried when include_cash=False")

    monkeypatch.setattr(portfolio_allocation, "_collect_portfolio_positions", fake_collect_positions)
    monkeypatch.setattr(portfolio_allocation, "get_cash_balance_impl", fail_cash_balance)
    monkeypatch.setattr(portfolio_allocation, "get_usd_krw_rate", lambda: 1400.0)
    monkeypatch.setattr(portfolio_allocation, "fetch_etf_all_cached", lambda: [])

    result = await portfolio_allocation.get_portfolio_allocation_impl(
        include_cash=False,
        include_positions=False,
        target_weights=None,
        drift_threshold_pct=5.0,
        is_mock=False,
    )

    assert result["summary"]["cash_value_krw"] == pytest.approx(0.0)
    assert result["cash"] == []
```

- [ ] **Step 3: Add KRX ETF degraded test**

Append:

```python
@pytest.mark.asyncio
async def test_get_portfolio_allocation_krx_etf_failure_is_degraded(monkeypatch) -> None:
    async def fake_collect_positions(**kwargs):
        return (
            [
                {
                    "account": "kis",
                    "account_name": "기본 계좌",
                    "broker": "kis",
                    "instrument_type": "equity_kr",
                    "market": "kr",
                    "symbol": "360750",
                    "name": "TIGER 미국S&P500",
                    "evaluation_amount": 100000.0,
                }
            ],
            [],
            None,
            None,
        )

    async def raise_krx():
        raise RuntimeError("KRX unavailable")

    monkeypatch.setattr(portfolio_allocation, "_collect_portfolio_positions", fake_collect_positions)
    monkeypatch.setattr(portfolio_allocation, "get_usd_krw_rate", lambda: 1400.0)
    monkeypatch.setattr(portfolio_allocation, "fetch_etf_all_cached", raise_krx)

    result = await portfolio_allocation.get_portfolio_allocation_impl(
        include_cash=False,
        include_positions=False,
        target_weights=None,
        drift_threshold_pct=5.0,
        is_mock=False,
    )

    assert result["errors"][0]["source"] == "krx_etf"
    by_class = {row["asset_class"]: row for row in result["asset_classes"]}
    assert by_class["kr_equity"]["value_krw"] == pytest.approx(100000.0)
    assert result["lookthrough"] == []
```

- [ ] **Step 4: Run edge-case tests**

Run:

```bash
uv run pytest --no-cov tests/test_mcp_portfolio_allocation.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit edge-case tests**

Run:

```bash
git add tests/test_mcp_portfolio_allocation.py
git commit -m "test: cover portfolio allocation routing and degraded paths"
```

## Task 5: Documentation

**Files:**
- Modify: `app/mcp_server/README.md`

- [ ] **Step 1: Add README spec near existing `get_holdings` spec**

Insert:

```markdown
### `get_portfolio_allocation` spec

Parameters:
- `account`: optional account filter matching `get_holdings` and `get_cash_balance` (`kis`, `upbit`, `toss`, `samsung_pension`, `isa`, `paper`, `paper:<name>`)
- `market`: optional holdings market filter (`kr`, `us`, `crypto`); cash is still included when `include_cash=true` unless `account` excludes the cash account
- `include_cash`: include cash balances in the allocation denominator, default `true`
- `include_positions`: include per-position normalized rows, default `false`
- `target_weights`: optional mapping from asset class to target percent; when omitted, no over/underweight flags are emitted
- `drift_threshold_pct`: threshold for `overweight` / `underweight` labels when `target_weights` is provided, default `5.0`
- `account_mode`: same routing selector as `get_holdings` (`db_simulated`, `kis_mock`, `kis_live`)

Behavior:
- Read-only only. The tool performs no order preview, order placement, mutation, reconciliation, or live approval action.
- Converts USD holdings and USD cash to KRW using the same exchange-rate service used by portfolio cash tools.
- Aggregates direct US equity as `us_equity`, KR equity as `kr_equity`, Upbit holdings as `crypto`, and cash as `cash`.
- Looks through KR-listed ETFs when KRX ETF metadata is available. KR ETFs classified as `미국주식` by `app.services.krx.classify_etf_category()` are counted as effective `us_equity`, while their surface account remains KR/KIS/Toss.
- Non-US foreign, commodity, bond, and unclear ETF categories are counted as `other` rather than Korean equity.
- If KRX ETF metadata lookup fails, the tool records a degraded `krx_etf` error and keeps KR ETF positions in their surface `kr_equity` bucket.
- Positions whose valuation is unavailable are excluded from the denominator and listed in `warnings` with `reason="position_value_unavailable"`.

Response shape:
- `summary`: KRW total, invested value, cash value, valued/unvalued position counts
- `asset_classes`: value, weight, direct/look-through split, target/drift fields, and optional weight status
- `accounts`: account-level KRW roll-up with asset-class children
- `lookthrough`: KR ETF rows whose effective exposure differs from surface exposure
- `positions`: returned only when `include_positions=true`
- `cash`: normalized cash rows when `include_cash=true`
- `errors`: broker, cash, exchange-rate, or KRX ETF partial failures
- `warnings`: non-fatal valuation omissions
```

- [ ] **Step 2: Run docs grep**

Run:

```bash
rg -n "get_portfolio_allocation|Cross-asset|look-through|lookthrough" app/mcp_server/README.md
```

Expected: README contains the new tool name and look-through behavior.

- [ ] **Step 3: Commit docs**

Run:

```bash
git add app/mcp_server/README.md
git commit -m "docs: document portfolio allocation MCP tool"
```

## Task 6: Final Verification

**Files:**
- No new files.

- [ ] **Step 1: Run focused test suite**

Run:

```bash
uv run pytest --no-cov \
  tests/services/test_portfolio_allocation_service.py \
  tests/test_mcp_portfolio_allocation.py \
  tests/test_mcp_tool_registration.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run lint on touched Python files**

Run:

```bash
uv run ruff check \
  app/services/portfolio_allocation_service.py \
  app/mcp_server/tooling/portfolio_allocation.py \
  app/mcp_server/tooling/portfolio_registration.py \
  tests/services/test_portfolio_allocation_service.py \
  tests/test_mcp_portfolio_allocation.py \
  tests/test_mcp_tool_registration.py \
  tests/_mcp_tooling_support.py
```

Expected: PASS.

- [ ] **Step 3: Run formatting check or formatter**

Run:

```bash
uv run ruff format --check \
  app/services/portfolio_allocation_service.py \
  app/mcp_server/tooling/portfolio_allocation.py \
  app/mcp_server/tooling/portfolio_registration.py \
  tests/services/test_portfolio_allocation_service.py \
  tests/test_mcp_portfolio_allocation.py \
  tests/test_mcp_tool_registration.py \
  tests/_mcp_tooling_support.py
```

Expected: PASS. If it fails, run the same command without `--check`, inspect the diff, then rerun the check.

- [ ] **Step 4: Confirm public MCP surface**

Run:

```bash
uv run pytest --no-cov tests/test_mcp_tool_registration.py::test_get_portfolio_allocation_registered_in_default_surface -q
```

Expected: PASS.

- [ ] **Step 5: Final commit**

Run:

```bash
git status --short
git log --oneline -5
```

Expected: only intentional files changed if commits were skipped during task execution, or a clean worktree if each task was committed.

## Self-Review

Spec coverage:
- Whole-account roll-up: Task 2 uses existing `_collect_portfolio_positions()` and `get_cash_balance_impl()`.
- Asset-class roll-up: Task 1 returns `asset_classes`.
- ETF look-through: Task 1 maps KR ETF metadata through `classify_etf_category()`, and Task 4 covers degraded metadata.
- Account-level PnL and roll-up: Task 1 includes per-account value and service-level `profit_loss_krw`; implementation may add account-level `profit_loss_krw` in the same aggregation block if consumers need it in the first response.
- FX/cash annotations: Task 1 and Task 2 include USD/KRW conversion and cash rows.
- Documentation: Task 5 updates `app/mcp_server/README.md`.

Placeholder scan:
- Forbidden placeholder phrases are absent from executable task steps.

Type consistency:
- MCP handler uses existing account-routing helpers and the existing portfolio collector return shape.
- Service consumes dict rows with the current `get_holdings` internal keys: `account`, `broker`, `account_name`, `instrument_type`, `market`, `symbol`, `name`, `evaluation_amount`, and `profit_loss`.

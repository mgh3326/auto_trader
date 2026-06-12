# ROB-532 Toss Portfolio Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Switch Toss portfolio read surfaces from `manual_holdings` reference-only data to Toss Open API data when `TOSS_API_ENABLED=true`, while preserving the existing manual path when the flag is off or the API read fails.

**Architecture:** Reuse the ROB-530 `TossReadClient` and add a small portfolio adapter that converts Toss holdings, buying power, and sellable quantity into existing MCP and Invest Home read models. `manual_holdings` remains as fallback and verification data; it is hidden only when Toss API succeeds for the same Toss symbol. No DB migration and no order mutation are introduced. Merge-gate update: Toss API rows remain read-only in ROB-532, so order-routability and orderable-capital signals stay false/zero until a separate Toss order path exists.

**Tech Stack:** Python 3.13, FastAPI service layer, MCP tooling, TaskIQ-safe async reads, dataclasses, Decimal, Pydantic v2 schemas, pytest, pytest-asyncio, Ruff, ty.

---

## Starting State And Scope

ROB-530 already added the read-only Toss API foundation:

- `app/services/brokers/toss/client.py` exposes `TossReadClient.holdings()`, `buying_power(currency=...)`, and `sellable_quantity(symbol=...)`.
- `app/core/config.py` has `toss_api_enabled`, client credentials, account sequence, and base URL settings.
- Toss is live-only, so all tests in this plan must use fake clients or `httpx.MockTransport`; do not call the real Toss API from pytest.

ROB-532 integrates that client into the portfolio surfaces:

- MCP `get_holdings`
- MCP `get_cash_balance` and downstream `get_available_capital`
- KR morning report cash display
- Invest Home `/invest/api/home` and `/invest/api/account-panel`
- MCP README documentation

Out of scope:

- No automatic Toss order adapter.
- No order preview/place/modify/cancel tool.
- No Toss ledger or reconcile tables.
- No Alembic migration.
- No Toss notification format change.
- No rebalance strategy change.

Risk labels to preserve for Linear/PR tracking:

- `high_risk_change`
- `needs_stronger_model_review`
- `hold_for_final_review`

Reason: this is read-only integration, but it changes live broker sellability/routability surfaces. It must not be merged, deployed, or used for live trading until stronger review clears the assumptions.

## Decisions To Preserve

- `TOSS_API_ENABLED=false` keeps current behavior exactly: Toss rows come from `manual_holdings`, `source="manual"` in MCP holdings, `source="toss_manual"` in Invest Home, `order_routable=false`, and `isTradeable=false`.
- `TOSS_API_ENABLED=true` makes Toss API the primary Toss portfolio source.
- If Toss API succeeds, hide Toss `manual_holdings` duplicates from normal output for the same normalized market/symbol pair. Keep non-Toss manual accounts such as Samsung pension and ISA visible.
- If Toss API fails, keep existing Toss manual rows visible and add a non-fatal warning/error entry for `source="toss_api"`.
- Do not deduplicate KIS and Toss holdings. If KIS and Toss both hold `005930`, both accounts stay visible because they are separate broker subaccounts.
- Toss symbols are already compatible with DB format. Preserve `BRK.B`; do not convert it to `BRK-B`, `BRK/B`, or another display format.
- Toss KRW buying power contributes to `get_cash_balance.summary.total_krw`. Toss USD buying power contributes to `get_cash_balance.summary.total_usd`. Toss API cash rows report `orderable=0.0`, so they do not inflate `get_available_capital.summary.total_orderable_krw` until Toss live-order tools exist.
- Toss API success may surface MCP per-position `sellable_quantity` from `/api/v1/sellable-quantity` as informational read data. Invest Home must keep Toss API holdings `isTradeable=false`, `sellableQuantity=0.0`, and `referenceQuantity=quantity` until a Toss order path exists.

## File Structure

- Create: `app/services/toss_portfolio_service.py`
  - Convert ROB-530 Toss DTOs into portfolio-domain dataclasses.
  - Fetch holdings, per-symbol sellable quantities, and KRW/USD buying power with one closeable read client.
  - Provide small helpers for MCP and Invest Home readers to share.
- Modify: `app/mcp_server/tooling/portfolio_holdings.py`
  - Add `_collect_toss_api_positions()`.
  - Insert Toss API collection behind `settings.toss_api_enabled`.
  - Hide same-symbol Toss manual rows only when Toss API collection succeeds.
- Modify: `app/mcp_server/tooling/portfolio_cash.py`
  - Add Toss cash rows behind `settings.toss_api_enabled`.
  - Include Toss cash in `get_cash_balance_impl()` so `get_available_capital_impl()` inherits the new rows.
- Modify: `app/mcp_server/tooling/order_validation.py`
  - Refresh the no-holdings sell message so Toss API provenance is explicit while remaining reference-only until a Toss order path exists.
- Modify: `app/schemas/invest_home.py`
  - Add `toss_api` to `AccountSourceLiteral`.
  - Keep manual defaults limited to `toss_manual`, `pension_manual`, and `isa_manual`.
- Modify: `app/services/invest_home_readers.py`
  - Add `TossApiHomeReader`.
  - Keep `ManualHomeReader` unchanged except for fallback use.
- Modify: `app/services/invest_home_service.py`
  - Include `toss_api` in home totals.
  - Accept an optional Toss API reader and choose Toss API primary/manual fallback behavior.
- Modify: `app/routers/invest_api.py`
  - Wire `TossApiHomeReader` into `InvestHomeService` when settings enable it.
- Modify: `app/services/n8n_kr_morning_report_service.py`
  - Replace Toss `"수동 관리"` cash with Toss buying-power values when enabled.
- Modify: `app/mcp_server/README.md`
  - Document Toss API holdings/cash behavior and fallback semantics.
- Create: `tests/test_toss_portfolio_service.py`
- Modify: `tests/test_mcp_portfolio_tools.py`
- Modify: `tests/test_order_sell_routability_message.py`
- Modify: `tests/test_invest_home_readers.py`
- Modify: `tests/test_invest_home_service.py`
- Modify: `tests/test_n8n_kr_morning_report.py`

## Task 1: Add Toss Portfolio Adapter

**Files:**
- Create: `app/services/toss_portfolio_service.py`
- Create: `tests/test_toss_portfolio_service.py`

- [ ] **Step 1: Write the failing adapter tests**

Add this test file:

```python
from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.toss.dto import (
    TossBuyingPower,
    TossHoldingItem,
    TossHoldings,
    TossSellableQuantity,
)
from app.services.toss_portfolio_service import fetch_toss_portfolio_snapshot


def _holding(
    *,
    symbol: str = "BRK.B",
    market_country: str = "US",
    currency: str = "USD",
    quantity: str = "1.5",
    sellable: str = "1.25",
) -> TossHoldingItem:
    return TossHoldingItem(
        symbol=symbol,
        name="Berkshire Hathaway B",
        market_country=market_country,
        currency=currency,
        quantity=Decimal(quantity),
        last_price=Decimal("430.12"),
        average_purchase_price=Decimal("400.00"),
        market_value={
            "amount": Decimal("645.18"),
            "amountAfterCost": Decimal("644.50"),
        },
        profit_loss={"amount": Decimal("45.18"), "rate": Decimal("0.0753")},
        daily_profit_loss={"amount": Decimal("1.20"), "rate": Decimal("0.0019")},
        cost={"commission": Decimal("0.68"), "tax": Decimal("0")},
    )


class _FakeTossClient:
    def __init__(self) -> None:
        self.closed = False
        self.sellable_calls: list[str] = []
        self.buying_power_calls: list[str] = []

    async def holdings(self) -> TossHoldings:
        return TossHoldings(items=[_holding()])

    async def sellable_quantity(self, *, symbol: str) -> TossSellableQuantity:
        self.sellable_calls.append(symbol)
        return TossSellableQuantity(sellable_quantity=Decimal("1.25"))

    async def buying_power(self, *, currency: str) -> TossBuyingPower:
        self.buying_power_calls.append(currency)
        amount = Decimal("123456") if currency == "KRW" else Decimal("789.01")
        return TossBuyingPower(currency=currency, cash_buying_power=amount)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_fetch_toss_portfolio_snapshot_maps_holdings_sellable_and_cash() -> None:
    client = _FakeTossClient()

    snapshot = await fetch_toss_portfolio_snapshot(client=client)

    assert client.closed is False
    assert client.sellable_calls == ["BRK.B"]
    assert client.buying_power_calls == ["KRW", "USD"]
    assert snapshot.cash_krw == Decimal("123456")
    assert snapshot.cash_usd == Decimal("789.01")
    assert len(snapshot.positions) == 1
    position = snapshot.positions[0]
    assert position.symbol == "BRK.B"
    assert position.instrument_type == "equity_us"
    assert position.market == "us"
    assert position.quantity == Decimal("1.5")
    assert position.sellable_quantity == Decimal("1.25")
    assert position.evaluation_amount == Decimal("645.18")
    assert position.profit_rate == Decimal("0.0753")


@pytest.mark.asyncio
async def test_fetch_toss_portfolio_snapshot_keeps_position_when_sellable_fails() -> None:
    class Client(_FakeTossClient):
        async def sellable_quantity(self, *, symbol: str) -> TossSellableQuantity:
            raise RuntimeError(f"sellable failed for {symbol}")

    snapshot = await fetch_toss_portfolio_snapshot(client=Client())

    assert snapshot.positions[0].sellable_quantity is None
    assert snapshot.errors == [
        {
            "source": "toss_api",
            "stage": "sellable_quantity",
            "symbol": "BRK.B",
            "error": "sellable failed for BRK.B",
        }
    ]


@pytest.mark.asyncio
async def test_fetch_toss_portfolio_snapshot_maps_kr_market() -> None:
    class Client(_FakeTossClient):
        async def holdings(self) -> TossHoldings:
            return TossHoldings(
                items=[
                    _holding(
                        symbol="005930",
                        market_country="KR",
                        currency="KRW",
                        quantity="10",
                    )
                ]
            )

    snapshot = await fetch_toss_portfolio_snapshot(client=Client())

    assert snapshot.positions[0].symbol == "005930"
    assert snapshot.positions[0].instrument_type == "equity_kr"
    assert snapshot.positions[0].market == "kr"
```

- [ ] **Step 2: Run the new adapter tests to verify failure**

Run:

```bash
uv run pytest tests/test_toss_portfolio_service.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.toss_portfolio_service'`.

- [ ] **Step 3: Implement the adapter module**

Create `app/services/toss_portfolio_service.py`:

```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from app.services.brokers.toss.client import TossReadClient


class TossPortfolioClient(Protocol):
    async def holdings(self) -> Any: ...
    async def sellable_quantity(self, *, symbol: str) -> Any: ...
    async def buying_power(self, *, currency: str) -> Any: ...
    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class TossPortfolioPosition:
    account: str
    account_name: str
    broker: str
    source: str
    instrument_type: str
    market: str
    symbol: str
    name: str
    quantity: Decimal
    avg_buy_price: Decimal
    current_price: Decimal
    evaluation_amount: Decimal | None
    profit_loss: Decimal | None
    profit_rate: Decimal | None
    sellable_quantity: Decimal | None


@dataclass(frozen=True)
class TossPortfolioSnapshot:
    positions: list[TossPortfolioPosition]
    cash_krw: Decimal | None = None
    cash_usd: Decimal | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)


def _instrument_type_for_market_country(market_country: str) -> str:
    normalized = market_country.strip().upper()
    if normalized == "KR":
        return "equity_kr"
    if normalized == "US":
        return "equity_us"
    raise ValueError(f"Unsupported Toss marketCountry: {market_country}")


def _market_for_instrument_type(instrument_type: str) -> str:
    if instrument_type == "equity_kr":
        return "kr"
    if instrument_type == "equity_us":
        return "us"
    raise ValueError(f"Unsupported Toss instrument_type: {instrument_type}")


def _decimal_dict_value(raw: dict[str, Any], key: str) -> Decimal | None:
    value = raw.get(key)
    return value if isinstance(value, Decimal) else None


async def fetch_toss_portfolio_snapshot(
    *,
    client: TossPortfolioClient | None = None,
) -> TossPortfolioSnapshot:
    created_client = client is None
    active_client: TossPortfolioClient = client or TossReadClient.from_settings()

    try:
        holdings = await active_client.holdings()
        errors: list[dict[str, Any]] = []

        sellable_results = await asyncio.gather(
            *[
                active_client.sellable_quantity(symbol=item.symbol)
                for item in holdings.items
            ],
            return_exceptions=True,
        )

        positions: list[TossPortfolioPosition] = []
        for item, sellable_result in zip(holdings.items, sellable_results, strict=True):
            sellable_quantity: Decimal | None = None
            if isinstance(sellable_result, BaseException):
                errors.append(
                    {
                        "source": "toss_api",
                        "stage": "sellable_quantity",
                        "symbol": item.symbol,
                        "error": str(sellable_result),
                    }
                )
            else:
                sellable_quantity = sellable_result.sellable_quantity

            instrument_type = _instrument_type_for_market_country(item.market_country)
            positions.append(
                TossPortfolioPosition(
                    account="toss",
                    account_name="Toss",
                    broker="toss",
                    source="toss_api",
                    instrument_type=instrument_type,
                    market=_market_for_instrument_type(instrument_type),
                    symbol=item.symbol.strip().upper(),
                    name=item.name or item.symbol,
                    quantity=item.quantity,
                    avg_buy_price=item.average_purchase_price,
                    current_price=item.last_price,
                    evaluation_amount=_decimal_dict_value(item.market_value, "amount"),
                    profit_loss=_decimal_dict_value(item.profit_loss, "amount"),
                    profit_rate=_decimal_dict_value(item.profit_loss, "rate"),
                    sellable_quantity=sellable_quantity,
                )
            )

        buying_power_results = await asyncio.gather(
            active_client.buying_power(currency="KRW"),
            active_client.buying_power(currency="USD"),
            return_exceptions=True,
        )
        cash_krw: Decimal | None = None
        cash_usd: Decimal | None = None
        for currency, result in zip(("KRW", "USD"), buying_power_results, strict=True):
            if isinstance(result, BaseException):
                errors.append(
                    {
                        "source": "toss_api",
                        "stage": "buying_power",
                        "currency": currency,
                        "error": str(result),
                    }
                )
                continue
            if result.currency == "KRW":
                cash_krw = result.cash_buying_power
            elif result.currency == "USD":
                cash_usd = result.cash_buying_power

        return TossPortfolioSnapshot(
            positions=positions,
            cash_krw=cash_krw,
            cash_usd=cash_usd,
            errors=errors,
        )
    finally:
        if created_client:
            await active_client.aclose()
```

- [ ] **Step 4: Run adapter tests to verify pass**

Run:

```bash
uv run pytest tests/test_toss_portfolio_service.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit adapter work**

Run:

```bash
git add app/services/toss_portfolio_service.py tests/test_toss_portfolio_service.py
git commit -m "feat(ROB-532): add Toss portfolio adapter"
```

## Task 2: Wire Toss API Into MCP get_holdings

**Files:**
- Modify: `app/mcp_server/tooling/portfolio_holdings.py`
- Modify: `tests/test_mcp_portfolio_tools.py`

- [ ] **Step 1: Add failing MCP holdings tests**

Add these tests near the existing `get_holdings` tests in `tests/test_mcp_portfolio_tools.py`:

```python
from decimal import Decimal

from app.services.toss_portfolio_service import (
    TossPortfolioPosition,
    TossPortfolioSnapshot,
)


@pytest.mark.asyncio
async def test_get_holdings_toss_api_enabled_adds_read_only_toss_account(monkeypatch):
    from app.mcp_server.tooling import portfolio_holdings

    async def fake_collect_kis_positions(*args, **kwargs):
        return [], []

    async def fake_collect_upbit_positions(*args, **kwargs):
        return [], []

    async def fake_collect_manual_positions(*args, **kwargs):
        return [], []

    async def fake_fetch_toss_snapshot():
        return TossPortfolioSnapshot(
            positions=[
                TossPortfolioPosition(
                    account="toss",
                    account_name="Toss",
                    broker="toss",
                    source="toss_api",
                    instrument_type="equity_us",
                    market="us",
                    symbol="BRK.B",
                    name="Berkshire Hathaway B",
                    quantity=Decimal("1.5"),
                    avg_buy_price=Decimal("400"),
                    current_price=Decimal("430.12"),
                    evaluation_amount=Decimal("645.18"),
                    profit_loss=Decimal("45.18"),
                    profit_rate=Decimal("0.0753"),
                    sellable_quantity=Decimal("1.25"),
                )
            ],
            cash_krw=Decimal("0"),
            cash_usd=Decimal("789.01"),
        )

    monkeypatch.setattr(portfolio_holdings.settings, "toss_api_enabled", True)
    monkeypatch.setattr(portfolio_holdings, "_collect_kis_positions", fake_collect_kis_positions)
    monkeypatch.setattr(portfolio_holdings, "_collect_upbit_positions", fake_collect_upbit_positions)
    monkeypatch.setattr(portfolio_holdings, "_collect_manual_positions", fake_collect_manual_positions)
    monkeypatch.setattr(portfolio_holdings, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot)

    result = await portfolio_holdings._get_holdings_impl(minimum_value=0)

    assert result["accounts"][0]["account"] == "toss"
    assert result["accounts"][0]["broker"] == "toss"
    assert result["accounts"][0]["order_routable"] is False
    assert result["accounts"][0]["positions"][0]["symbol"] == "BRK.B"
    assert result["accounts"][0]["positions"][0]["sellable_quantity"] == 1.25


@pytest.mark.asyncio
async def test_get_holdings_toss_api_success_hides_duplicate_toss_manual(monkeypatch):
    from app.mcp_server.tooling import portfolio_holdings

    async def fake_collect_kis_positions(*args, **kwargs):
        return [], []

    async def fake_collect_upbit_positions(*args, **kwargs):
        return [], []

    async def fake_collect_manual_positions(*args, **kwargs):
        return [
            {
                "account": "toss",
                "account_name": "Toss 수동",
                "broker": "toss",
                "source": "manual",
                "instrument_type": "equity_us",
                "market": "us",
                "symbol": "BRK.B",
                "name": "Berkshire Hathaway B",
                "quantity": 1.5,
                "avg_buy_price": 400.0,
                "current_price": 430.12,
                "evaluation_amount": 645.18,
                "profit_loss": 45.18,
                "profit_rate": 0.0753,
            }
        ], []

    async def fake_fetch_toss_snapshot():
        return TossPortfolioSnapshot(
            positions=[
                TossPortfolioPosition(
                    account="toss",
                    account_name="Toss",
                    broker="toss",
                    source="toss_api",
                    instrument_type="equity_us",
                    market="us",
                    symbol="BRK.B",
                    name="Berkshire Hathaway B",
                    quantity=Decimal("1.5"),
                    avg_buy_price=Decimal("400"),
                    current_price=Decimal("430.12"),
                    evaluation_amount=Decimal("645.18"),
                    profit_loss=Decimal("45.18"),
                    profit_rate=Decimal("0.0753"),
                    sellable_quantity=Decimal("1.25"),
                )
            ]
        )

    monkeypatch.setattr(portfolio_holdings.settings, "toss_api_enabled", True)
    monkeypatch.setattr(portfolio_holdings, "_collect_kis_positions", fake_collect_kis_positions)
    monkeypatch.setattr(portfolio_holdings, "_collect_upbit_positions", fake_collect_upbit_positions)
    monkeypatch.setattr(portfolio_holdings, "_collect_manual_positions", fake_collect_manual_positions)
    monkeypatch.setattr(portfolio_holdings, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot)

    result = await portfolio_holdings._get_holdings_impl(minimum_value=0)

    accounts = result["accounts"]
    assert len(accounts) == 1
    assert accounts[0]["account"] == "toss"
    assert accounts[0]["positions"][0]["source"] == "toss_api"


@pytest.mark.asyncio
async def test_get_holdings_toss_api_failure_keeps_manual_fallback(monkeypatch):
    from app.mcp_server.tooling import portfolio_holdings

    async def fake_collect_kis_positions(*args, **kwargs):
        return [], []

    async def fake_collect_upbit_positions(*args, **kwargs):
        return [], []

    async def fake_collect_manual_positions(*args, **kwargs):
        return [
            {
                "account": "toss",
                "account_name": "Toss 수동",
                "broker": "toss",
                "source": "manual",
                "instrument_type": "equity_kr",
                "market": "kr",
                "symbol": "005930",
                "name": "삼성전자",
                "quantity": 10.0,
                "avg_buy_price": 65000.0,
                "current_price": 70000.0,
                "evaluation_amount": 700000.0,
                "profit_loss": 50000.0,
                "profit_rate": 0.0769,
            }
        ], []

    async def fake_fetch_toss_snapshot():
        raise RuntimeError("toss unavailable")

    monkeypatch.setattr(portfolio_holdings.settings, "toss_api_enabled", True)
    monkeypatch.setattr(portfolio_holdings, "_collect_kis_positions", fake_collect_kis_positions)
    monkeypatch.setattr(portfolio_holdings, "_collect_upbit_positions", fake_collect_upbit_positions)
    monkeypatch.setattr(portfolio_holdings, "_collect_manual_positions", fake_collect_manual_positions)
    monkeypatch.setattr(portfolio_holdings, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot)

    result = await portfolio_holdings._get_holdings_impl(minimum_value=0)

    assert result["accounts"][0]["order_routable"] is False
    assert result["accounts"][0]["positions"][0]["source"] == "manual"
    assert {"source": "toss_api", "error": "toss unavailable"} in result["errors"]
```

- [ ] **Step 2: Run the new MCP holdings tests to verify failure**

Run:

```bash
uv run pytest \
  tests/test_mcp_portfolio_tools.py::test_get_holdings_toss_api_enabled_adds_read_only_toss_account \
  tests/test_mcp_portfolio_tools.py::test_get_holdings_toss_api_success_hides_duplicate_toss_manual \
  tests/test_mcp_portfolio_tools.py::test_get_holdings_toss_api_failure_keeps_manual_fallback \
  -q
```

Expected: FAIL because `portfolio_holdings` does not import `settings` or `fetch_toss_portfolio_snapshot`, and Toss API collection is not wired.

- [ ] **Step 3: Implement Toss API collection**

Modify `app/mcp_server/tooling/portfolio_holdings.py` imports:

```python
from app.core.config import settings
from app.services.toss_portfolio_service import (
    TossPortfolioPosition,
    fetch_toss_portfolio_snapshot,
)
```

Add helpers near `_collect_manual_positions()`:

```python
def _same_toss_symbol(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        str(left.get("broker") or "").lower() == "toss"
        and str(right.get("broker") or "").lower() == "toss"
        and str(left.get("instrument_type") or "") == str(right.get("instrument_type") or "")
        and _normalize_position_symbol(
            str(left.get("symbol") or ""),
            str(left.get("instrument_type") or ""),
        )
        == _normalize_position_symbol(
            str(right.get("symbol") or ""),
            str(right.get("instrument_type") or ""),
        )
    )


def _toss_api_position_to_mcp(position: TossPortfolioPosition) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "account": position.account,
        "account_name": position.account_name,
        "broker": position.broker,
        "source": position.source,
        "instrument_type": position.instrument_type,
        "market": position.market,
        "symbol": position.symbol,
        "name": position.name,
        "quantity": float(position.quantity),
        "avg_buy_price": float(position.avg_buy_price),
        "current_price": float(position.current_price),
        "evaluation_amount": float(position.evaluation_amount)
        if position.evaluation_amount is not None
        else None,
        "profit_loss": float(position.profit_loss)
        if position.profit_loss is not None
        else None,
        "profit_rate": float(position.profit_rate)
        if position.profit_rate is not None
        else None,
    }
    if position.sellable_quantity is not None:
        payload["sellable_quantity"] = float(position.sellable_quantity)
    return payload


async def _collect_toss_api_positions(
    market_filter: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    if not bool(getattr(settings, "toss_api_enabled", False)):
        return [], [], False
    if market_filter == "crypto":
        return [], [], False

    try:
        snapshot = await fetch_toss_portfolio_snapshot()
    except Exception as exc:
        return [], [{"source": "toss_api", "error": str(exc)}], False

    positions = [
        _toss_api_position_to_mcp(position)
        for position in snapshot.positions
        if market_filter is None or position.instrument_type == market_filter
    ]
    return positions, snapshot.errors, True
```

Modify `_collect_portfolio_positions()` after manual and Toss tasks have been gathered:

```python
    toss_api_positions: list[dict[str, Any]] = []
    toss_api_errors: list[dict[str, Any]] = []
    toss_api_succeeded = False
    if bool(getattr(settings, "toss_api_enabled", False)):
        toss_api_positions, toss_api_errors, toss_api_succeeded = (
            await _collect_toss_api_positions(market_filter)
        )
        positions.extend(toss_api_positions)
        errors.extend(toss_api_errors)

    if toss_api_succeeded and toss_api_positions:
        positions = [
            position
            for position in positions
            if not (
                position.get("source") == "manual"
                and str(position.get("broker") or "").lower() == "toss"
                and any(
                    _same_toss_symbol(position, toss_position)
                    for toss_position in toss_api_positions
                )
            )
        ]
```

Place the block after the existing `asyncio.gather(*tasks, return_exceptions=True)` result merge and before `market_filter` / `account_filter` filtering.

- [ ] **Step 4: Expose sellable quantity in position output**

Inspect `app/mcp_server/tooling/portfolio_helpers.py::position_to_output`. If it currently drops unknown keys, add this conditional:

```python
    if "sellable_quantity" in position:
        output["sellable_quantity"] = position["sellable_quantity"]
```

Use the existing output variable name in that helper. Keep the response key snake_case because MCP holdings output already uses snake_case.

- [ ] **Step 5: Run the MCP holdings tests to verify pass**

Run:

```bash
uv run pytest \
  tests/test_mcp_portfolio_tools.py::test_get_holdings_toss_api_enabled_adds_read_only_toss_account \
  tests/test_mcp_portfolio_tools.py::test_get_holdings_toss_api_success_hides_duplicate_toss_manual \
  tests/test_mcp_portfolio_tools.py::test_get_holdings_toss_api_failure_keeps_manual_fallback \
  -q
```

Expected: PASS.

- [ ] **Step 6: Run nearby holdings regression tests**

Run:

```bash
uv run pytest tests/test_mcp_portfolio_tools.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit MCP holdings work**

Run:

```bash
git add app/mcp_server/tooling/portfolio_holdings.py app/mcp_server/tooling/portfolio_helpers.py tests/test_mcp_portfolio_tools.py
git commit -m "feat(ROB-532): route Toss API holdings into MCP portfolio"
```

## Task 3: Wire Toss API Into get_cash_balance and get_available_capital

**Files:**
- Modify: `app/mcp_server/tooling/portfolio_cash.py`
- Modify: `tests/test_mcp_portfolio_tools.py`

- [ ] **Step 1: Add failing cash tests**

Add these tests near the `get_cash_balance` tests:

```python
from decimal import Decimal

from app.services.toss_portfolio_service import TossPortfolioSnapshot


@pytest.mark.asyncio
async def test_get_cash_balance_toss_api_enabled_adds_krw_and_usd(monkeypatch):
    from app.mcp_server.tooling import portfolio_cash

    async def fake_fetch_toss_snapshot():
        return TossPortfolioSnapshot(
            positions=[],
            cash_krw=Decimal("123456"),
            cash_usd=Decimal("789.01"),
        )

    monkeypatch.setattr(portfolio_cash.settings, "toss_api_enabled", True)
    monkeypatch.setattr(portfolio_cash.upbit_service, "fetch_krw_cash_summary", AsyncMock(side_effect=RuntimeError("skip upbit")))
    monkeypatch.setattr(portfolio_cash, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot)

    result = await portfolio_cash.get_cash_balance_impl(account="toss")

    assert result["accounts"] == [
        {
            "account": "toss",
            "account_name": "Toss",
            "broker": "toss",
            "currency": "KRW",
            "balance": 123456.0,
            "orderable": 123456.0,
            "formatted": "123,456 KRW",
        },
        {
            "account": "toss",
            "account_name": "Toss",
            "broker": "toss",
            "currency": "USD",
            "balance": 789.01,
            "orderable": 789.01,
            "formatted": "789.01 USD",
        },
    ]
    assert result["summary"] == {"total_krw": 123456.0, "total_usd": 789.01}
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_get_cash_balance_toss_api_failure_is_strict_for_toss_filter(monkeypatch):
    from app.mcp_server.tooling import portfolio_cash

    async def fake_fetch_toss_snapshot():
        raise RuntimeError("toss cash unavailable")

    monkeypatch.setattr(portfolio_cash.settings, "toss_api_enabled", True)
    monkeypatch.setattr(portfolio_cash, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot)

    with pytest.raises(RuntimeError, match="Toss cash balance query failed"):
        await portfolio_cash.get_cash_balance_impl(account="toss")
```

If `AsyncMock` is not already imported in `tests/test_mcp_portfolio_tools.py`, add:

```python
from unittest.mock import AsyncMock
```

- [ ] **Step 2: Run the new cash tests to verify failure**

Run:

```bash
uv run pytest \
  tests/test_mcp_portfolio_tools.py::test_get_cash_balance_toss_api_enabled_adds_krw_and_usd \
  tests/test_mcp_portfolio_tools.py::test_get_cash_balance_toss_api_failure_is_strict_for_toss_filter \
  -q
```

Expected: FAIL because `portfolio_cash` does not import `settings` or `fetch_toss_portfolio_snapshot`.

- [ ] **Step 3: Implement Toss cash rows**

Modify imports in `app/mcp_server/tooling/portfolio_cash.py`:

```python
from decimal import Decimal

from app.core.config import settings
from app.services.toss_portfolio_service import fetch_toss_portfolio_snapshot
```

Add helpers near `get_cash_balance_impl()`:

```python
def _decimal_to_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _format_cash_amount(value: float, currency: str) -> str:
    if currency == "KRW":
        return f"{int(value):,} KRW"
    return f"{value:,.2f} {currency}"
```

Inside `get_cash_balance_impl()`, after `strict_mode = account_filter is not None`, add:

```python
    if account_filter is None or account_filter == "toss":
        if bool(getattr(settings, "toss_api_enabled", False)):
            try:
                toss_snapshot = await fetch_toss_portfolio_snapshot()
                toss_krw = _decimal_to_float(toss_snapshot.cash_krw)
                toss_usd = _decimal_to_float(toss_snapshot.cash_usd)
                if toss_krw is not None:
                    accounts.append(
                        {
                            "account": "toss",
                            "account_name": "Toss",
                            "broker": "toss",
                            "currency": "KRW",
                            "balance": toss_krw,
                            "orderable": 0.0,
                            "formatted": _format_cash_amount(toss_krw, "KRW"),
                        }
                    )
                    total_krw += toss_krw
                if toss_usd is not None:
                    accounts.append(
                        {
                            "account": "toss",
                            "account_name": "Toss",
                            "broker": "toss",
                            "currency": "USD",
                            "balance": toss_usd,
                            "orderable": 0.0,
                            "formatted": _format_cash_amount(toss_usd, "USD"),
                        }
                    )
                    total_usd += toss_usd
                errors.extend(toss_snapshot.errors)
            except Exception as exc:
                if strict_mode:
                    raise RuntimeError(
                        f"Toss cash balance query failed: {exc}"
                    ) from exc
                errors.append({"source": "toss_api", "market": "cash", "error": str(exc)})
```

Place this block before Upbit/KIS collection. This keeps `account="toss"` strict and lets `account=None` return partial failures without failing the whole cash call.

- [ ] **Step 4: Run the cash tests to verify pass**

Run:

```bash
uv run pytest \
  tests/test_mcp_portfolio_tools.py::test_get_cash_balance_toss_api_enabled_adds_krw_and_usd \
  tests/test_mcp_portfolio_tools.py::test_get_cash_balance_toss_api_failure_is_strict_for_toss_filter \
  -q
```

Expected: PASS.

- [ ] **Step 5: Run available-capital focused tests**

Run:

```bash
uv run pytest tests/test_mcp_available_capital.py -q
```

Expected: PASS. If tests assert exact account lists, update only the Toss-enabled test paths; flag-off behavior must remain unchanged.

- [ ] **Step 6: Commit cash work**

Run:

```bash
git add app/mcp_server/tooling/portfolio_cash.py tests/test_mcp_portfolio_tools.py tests/test_mcp_available_capital.py
git commit -m "feat(ROB-532): include Toss API buying power in portfolio cash"
```

## Task 4: Update Sell-Side Holdings Miss Message

**Files:**
- Modify: `app/mcp_server/tooling/order_validation.py`
- Modify: `tests/test_order_sell_routability_message.py`

- [ ] **Step 1: Add failing message tests**

Modify `tests/test_order_sell_routability_message.py` to include:

```python
def test_no_holdings_sell_message_mentions_toss_api_when_enabled(monkeypatch):
    from app.mcp_server.tooling import order_validation

    monkeypatch.setattr(order_validation.settings, "toss_api_enabled", True)

    msg = order_validation._no_holdings_sell_message("005930", "equity_kr", False)

    assert "KIS subaccount" in msg
    assert "Toss API" in msg
    assert "reference-only" in msg


def test_no_holdings_sell_message_preserves_reference_only_when_disabled(monkeypatch):
    from app.mcp_server.tooling import order_validation

    monkeypatch.setattr(order_validation.settings, "toss_api_enabled", False)

    msg = order_validation._no_holdings_sell_message("005930", "equity_kr", False)

    assert "toss/samsung" in msg
    assert "reference-only" in msg
```

- [ ] **Step 2: Run message tests to verify failure**

Run:

```bash
uv run pytest tests/test_order_sell_routability_message.py -q
```

Expected: FAIL because `order_validation` does not import `settings` and the message is static.

- [ ] **Step 3: Implement flag-aware message**

Modify imports in `app/mcp_server/tooling/order_validation.py`:

```python
from app.core.config import settings
```

Change `_no_holdings_sell_message()`:

```python
    if bool(getattr(settings, "toss_api_enabled", False)):
        return (
            f"No sellable holdings for {symbol} in the KIS subaccount that "
            f"{channel} routes to. If this symbol is held at Toss, use the "
            "Toss API order path after Toss live-order tools are enabled; "
            "manual Samsung/legacy holdings remain reference-only. Check "
            "get_holdings 'order_routable'/'account_mode'."
        )
```

Keep the existing disabled message as the `else` branch.

- [ ] **Step 4: Run message tests to verify pass**

Run:

```bash
uv run pytest tests/test_order_sell_routability_message.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit message work**

Run:

```bash
git add app/mcp_server/tooling/order_validation.py tests/test_order_sell_routability_message.py
git commit -m "fix(ROB-532): clarify Toss sell routability message"
```

## Task 5: Add Toss API Source To Invest Home Schemas And Reader

**Files:**
- Modify: `app/schemas/invest_home.py`
- Modify: `app/services/invest_home_readers.py`
- Modify: `tests/test_invest_home_readers.py`

- [ ] **Step 1: Add failing reader test**

Add to `tests/test_invest_home_readers.py`:

```python
from decimal import Decimal

from app.services.toss_portfolio_service import (
    TossPortfolioPosition,
    TossPortfolioSnapshot,
)


@pytest.mark.asyncio
async def test_toss_api_home_reader_maps_read_only_holdings_and_cash(monkeypatch):
    from app.services import invest_home_readers as readers

    async def fake_fetch_toss_snapshot():
        return TossPortfolioSnapshot(
            positions=[
                TossPortfolioPosition(
                    account="toss",
                    account_name="Toss",
                    broker="toss",
                    source="toss_api",
                    instrument_type="equity_us",
                    market="us",
                    symbol="BRK.B",
                    name="Berkshire Hathaway B",
                    quantity=Decimal("1.5"),
                    avg_buy_price=Decimal("400"),
                    current_price=Decimal("430.12"),
                    evaluation_amount=Decimal("645.18"),
                    profit_loss=Decimal("45.18"),
                    profit_rate=Decimal("0.0753"),
                    sellable_quantity=Decimal("1.25"),
                )
            ],
            cash_krw=Decimal("123456"),
            cash_usd=Decimal("789.01"),
        )

    monkeypatch.setattr(readers, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot)

    result = await readers.TossApiHomeReader().fetch(user_id=1)

    assert result.warning is None
    assert result.accounts[0].source == "toss_api"
    assert result.accounts[0].accountKind == "live"
    assert result.accounts[0].cashBalances.krw == 123456.0
    assert result.accounts[0].cashBalances.usd == 789.01
    assert result.accounts[0].buyingPower.krw is None
    assert result.accounts[0].buyingPower.usd is None
    holding = result.holdings[0]
    assert holding.source == "toss_api"
    assert holding.sourceOfTruth is True
    assert holding.isTradeable is False
    assert holding.manualOnly is False
    assert holding.sellableQuantity == 0.0
    assert holding.referenceQuantity == 1.5
```

- [ ] **Step 2: Run reader test to verify failure**

Run:

```bash
uv run pytest tests/test_invest_home_readers.py::test_toss_api_home_reader_maps_read_only_holdings_and_cash -q
```

Expected: FAIL because `AccountSourceLiteral` does not allow `toss_api` and `TossApiHomeReader` does not exist.

- [ ] **Step 3: Add `toss_api` to schema source literal**

Modify `app/schemas/invest_home.py`:

```python
AccountSourceLiteral = Literal[
    "kis",
    "upbit",
    "toss_manual",
    "toss_api",
    "pension_manual",
    "isa_manual",
    "kis_mock",
    "kiwoom_mock",
    "alpaca_paper",
    "db_simulated",
]
```

Do not add `toss_api` to the manual-default source set inside `Holding.apply_source_separation_defaults()`.

- [ ] **Step 4: Implement `TossApiHomeReader`**

Modify imports in `app/services/invest_home_readers.py`:

```python
from app.services.toss_portfolio_service import fetch_toss_portfolio_snapshot
```

Add this reader before `ManualHomeReader`:

```python
class TossApiHomeReader:
    """Toss Open API live portfolio reader."""

    async def fetch(self, *, user_id: int) -> _SourceFetchResult:
        del user_id
        try:
            snapshot = await fetch_toss_portfolio_snapshot()
            holdings: list[Holding] = []
            value_krw_total = 0.0
            cost_basis_krw_total: float | None = 0.0
            pnl_krw_total: float | None = 0.0

            for position in snapshot.positions:
                currency = "KRW" if position.instrument_type == "equity_kr" else "USD"
                market = "KR" if position.instrument_type == "equity_kr" else "US"
                value_native = (
                    float(position.evaluation_amount)
                    if position.evaluation_amount is not None
                    else None
                )
                value_krw = value_native if currency == "KRW" else None
                pnl_krw = (
                    float(position.profit_loss)
                    if currency == "KRW" and position.profit_loss is not None
                    else None
                )
                cost_basis = float(position.quantity * position.avg_buy_price)
                if value_krw is not None:
                    value_krw_total += value_krw
                else:
                    cost_basis_krw_total = None
                    pnl_krw_total = None
                if cost_basis_krw_total is not None and currency == "KRW":
                    cost_basis_krw_total += cost_basis
                elif currency != "KRW":
                    cost_basis_krw_total = None
                if pnl_krw_total is not None and pnl_krw is not None:
                    pnl_krw_total += pnl_krw
                elif currency != "KRW":
                    pnl_krw_total = None

                holdings.append(
                    Holding(
                        holdingId=f"toss_api:{position.symbol}",
                        accountId="toss_api_account",
                        source="toss_api",
                        accountKind="live",
                        symbol=position.symbol,
                        market=market,
                        assetType="equity",
                        assetCategory="kr_stock" if market == "KR" else "us_stock",
                        displayName=position.name,
                        quantity=float(position.quantity),
                        averageCost=float(position.avg_buy_price),
                        costBasis=cost_basis,
                        currency=currency,
                        valueNative=value_native,
                        valueKrw=value_krw,
                        pnlKrw=pnl_krw,
                        pnlRate=float(position.profit_rate)
                        if position.profit_rate is not None
                        else None,
                        priceState="live",
                        sourceOfTruth=True,
                        isTradeable=False,
                        manualOnly=False,
                        sellableQuantity=0.0,
                        pendingSellQuantity=0.0,
                        referenceQuantity=float(position.quantity),
                    )
                )

            pnl_rate: float | None = None
            if cost_basis_krw_total and cost_basis_krw_total > 0 and pnl_krw_total is not None:
                pnl_rate = pnl_krw_total / cost_basis_krw_total

            account = Account(
                accountId="toss_api_account",
                displayName="Toss",
                source="toss_api",
                accountKind="live",
                includedInHome=True,
                valueKrw=value_krw_total,
                costBasisKrw=cost_basis_krw_total,
                pnlKrw=pnl_krw_total,
                pnlRate=pnl_rate,
                cashBalances=CashAmounts(
                    krw=float(snapshot.cash_krw) if snapshot.cash_krw is not None else None,
                    usd=float(snapshot.cash_usd) if snapshot.cash_usd is not None else None,
                ),
                buyingPower=CashAmounts(),
            )
            warning = None
            if snapshot.errors:
                warning = InvestHomeWarning(
                    source="toss_api",
                    message="; ".join(str(item.get("error")) for item in snapshot.errors),
                )
            return _SourceFetchResult(
                accounts=[account],
                holdings=holdings,
                warning=warning,
            )
        except Exception as exc:
            logger.warning("Toss API fetch failed: %s", exc, exc_info=True)
            return _SourceFetchResult(
                accounts=[],
                holdings=[],
                warning=InvestHomeWarning(source="toss_api", message=str(exc)),
            )
```

- [ ] **Step 5: Run reader test to verify pass**

Run:

```bash
uv run pytest tests/test_invest_home_readers.py::test_toss_api_home_reader_maps_read_only_holdings_and_cash -q
```

Expected: PASS.

- [ ] **Step 6: Commit reader work**

Run:

```bash
git add app/schemas/invest_home.py app/services/invest_home_readers.py tests/test_invest_home_readers.py
git commit -m "feat(ROB-532): add Toss API invest home reader"
```

## Task 6: Make InvestHomeService Prefer Toss API With Manual Fallback

**Files:**
- Modify: `app/services/invest_home_service.py`
- Modify: `app/routers/invest_api.py`
- Modify: `tests/test_invest_home_service.py`

- [ ] **Step 1: Add failing service tests**

Add to `tests/test_invest_home_service.py`:

```python
@pytest.mark.asyncio
async def test_get_home_uses_toss_api_instead_of_manual_when_toss_api_has_holdings():
    toss_api_reader = _Reader(
        holdings=[
            Holding(
                holdingId="toss_api:BRK.B",
                accountId="toss_api_account",
                source="toss_api",
                accountKind="live",
                symbol="BRK.B",
                market="US",
                assetType="equity",
                assetCategory="us_stock",
                displayName="Berkshire Hathaway B",
                quantity=1.5,
                averageCost=400.0,
                costBasis=600.0,
                currency="USD",
                valueNative=645.18,
                valueKrw=None,
                sourceOfTruth=True,
                isTradeable=False,
                manualOnly=False,
                sellableQuantity=0.0,
                referenceQuantity=1.5,
            )
        ]
    )
    manual_reader = _Reader(
        holdings=[
            Holding(
                holdingId="manual:1",
                accountId="1",
                source="toss_manual",
                accountKind="manual",
                symbol="BRK.B",
                market="US",
                assetType="equity",
                assetCategory="us_stock",
                displayName="Berkshire Hathaway B",
                quantity=1.5,
                averageCost=400.0,
                costBasis=600.0,
                currency="USD",
                manualOnly=True,
            )
        ]
    )
    service = InvestHomeService(
        kis_reader=_Reader(),
        upbit_reader=_Reader(),
        manual_reader=manual_reader,
        toss_api_reader=toss_api_reader,
    )

    result = await service.get_home(user_id=1)

    assert [h.source for h in result.holdings] == ["toss_api"]
    assert result.groupedHoldings[0].tradeableQuantity == 0.0
    assert result.groupedHoldings[0].sellableQuantity == 0.0
    assert result.groupedHoldings[0].referenceQuantity == 1.5


@pytest.mark.asyncio
async def test_get_home_falls_back_to_manual_when_toss_api_returns_warning_only():
    service = InvestHomeService(
        kis_reader=_Reader(),
        upbit_reader=_Reader(),
        manual_reader=_Reader(
            holdings=[
                Holding(
                    holdingId="manual:1",
                    accountId="1",
                    source="toss_manual",
                    accountKind="manual",
                    symbol="005930",
                    market="KR",
                    assetType="equity",
                    assetCategory="kr_stock",
                    displayName="삼성전자",
                    quantity=10.0,
                    averageCost=65000.0,
                    costBasis=650000.0,
                    currency="KRW",
                    valueNative=700000.0,
                    valueKrw=700000.0,
                    manualOnly=True,
                )
            ]
        ),
        toss_api_reader=_Reader(
            warning=InvestHomeWarning(source="toss_api", message="toss unavailable")
        ),
    )

    result = await service.get_home(user_id=1)

    assert [h.source for h in result.holdings] == ["toss_manual"]
    assert any(w.source == "toss_api" for w in result.meta.warnings)
```

Use the existing `_Reader` fixture/helper in the file. If that helper has a different constructor, adapt the fixture setup while preserving the asserted behavior above.

- [ ] **Step 2: Run service tests to verify failure**

Run:

```bash
uv run pytest \
  tests/test_invest_home_service.py::test_get_home_uses_toss_api_instead_of_manual_when_toss_api_has_holdings \
  tests/test_invest_home_service.py::test_get_home_falls_back_to_manual_when_toss_api_returns_warning_only \
  -q
```

Expected: FAIL because `InvestHomeService.__init__()` does not accept `toss_api_reader`.

- [ ] **Step 3: Implement service selection**

Modify constants in `app/services/invest_home_service.py`:

```python
HOME_INCLUDED_SOURCES: frozenset[str] = frozenset(
    {"kis", "upbit", "toss_manual", "toss_api"}
)
```

Keep `_MANUAL` unchanged:

```python
_MANUAL: frozenset[str] = frozenset({"toss_manual", "pension_manual", "isa_manual"})
```

Modify `InvestHomeService.__init__()`:

```python
        toss_api_reader=None,
```

and assign:

```python
        self._toss_api = toss_api_reader
```

Replace the live/manual reader loop in both `get_home()` and `get_account_panel()` with this sequence:

```python
            (self._kis.fetch, "kis"),
            (self._upbit.fetch, "upbit"),
```

After that loop, add a Toss branch:

```python
        toss_api_result: _SourceFetchResult | None = None
        if self._toss_api is not None:
            try:
                toss_api_result = await self._toss_api.fetch(user_id=user_id)
                if toss_api_result.warning is not None:
                    warnings.append(toss_api_result.warning)
                if toss_api_result.holdings or toss_api_result.accounts:
                    accounts.extend(toss_api_result.accounts)
                    holdings.extend(toss_api_result.holdings)
            except Exception as exc:
                logger.warning("[invest_home] toss_api fetch failed: %s", exc, exc_info=True)
                warnings.append(InvestHomeWarning(source="toss_api", message=str(exc)))

        if toss_api_result is None or not (
            toss_api_result.holdings or toss_api_result.accounts
        ):
            result = await self._manual.fetch(user_id=user_id)
            accounts.extend(result.accounts)
            holdings.extend(result.holdings)
            if result.warning is not None:
                warnings.append(result.warning)
            toss_account = build_manual_account_from_holdings(result.holdings)
            if toss_account is not None:
                accounts.append(toss_account)
```

Keep existing Sentry spans by wrapping the Toss branch in `sentry_sdk.start_span(op="invest.home.reader", name="invest.home.toss_api")` and the manual fallback in `name="invest.home.manual"` if the current codebase expects spans in tests.

- [ ] **Step 4: Wire the reader in the router**

Modify imports in `app/routers/invest_api.py`:

```python
from app.core.config import settings
```

and include `TossApiHomeReader` in the local reader imports.

Modify the service factory:

```python
    return InvestHomeService(
        kis_reader=KISHomeReader(db),
        upbit_reader=UpbitHomeReader(db),
        manual_reader=ManualHomeReader(db, quote_service=quote_service),
        toss_api_reader=TossApiHomeReader()
        if bool(getattr(settings, "toss_api_enabled", False))
        else None,
        paper_readers=paper_readers,
    )
```

- [ ] **Step 5: Run service tests to verify pass**

Run:

```bash
uv run pytest \
  tests/test_invest_home_service.py::test_get_home_uses_toss_api_instead_of_manual_when_toss_api_has_holdings \
  tests/test_invest_home_service.py::test_get_home_falls_back_to_manual_when_toss_api_returns_warning_only \
  -q
```

Expected: PASS.

- [ ] **Step 6: Run Invest Home suite**

Run:

```bash
uv run pytest tests/test_invest_home_readers.py tests/test_invest_home_service.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit Invest Home service work**

Run:

```bash
git add app/services/invest_home_service.py app/routers/invest_api.py tests/test_invest_home_service.py
git commit -m "feat(ROB-532): prefer Toss API in invest home"
```

## Task 7: Update KR Morning Report Toss Cash

**Files:**
- Modify: `app/services/n8n_kr_morning_report_service.py`
- Modify: `tests/test_n8n_kr_morning_report.py`

- [ ] **Step 1: Add failing morning report cash test**

Add to `tests/test_n8n_kr_morning_report.py`:

```python
from decimal import Decimal

from app.services.toss_portfolio_service import TossPortfolioSnapshot


@pytest.mark.asyncio
async def test_kr_morning_report_includes_toss_api_cash(monkeypatch):
    import app.services.n8n_kr_morning_report_service as service

    monkeypatch.setattr(service.settings, "toss_api_enabled", True)

    async def fake_fetch_toss_snapshot():
        return TossPortfolioSnapshot(
            positions=[],
            cash_krw=Decimal("123456"),
            cash_usd=Decimal("789.01"),
        )

    monkeypatch.setattr(service, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot)
    monkeypatch.setattr(service, "_get_portfolio_overview", AsyncMock(return_value={"holdings": [], "warnings": []}))
    monkeypatch.setattr(service, "_fetch_kis_cash_balance", AsyncMock(return_value=1000000.0))
    monkeypatch.setattr(service, "fetch_pending_orders", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_fetch_screening", AsyncMock(return_value={"results": []}))

    payload = await service.build_kr_morning_report_payload(top_n=3)

    assert payload["cash_balance"]["kis_krw"] == 1000000.0
    assert payload["cash_balance"]["toss_krw"] == 123456.0
    assert payload["cash_balance"]["toss_usd"] == 789.01
    assert payload["cash_balance"]["total_krw"] == 1123456.0
    assert payload["cash_balance"]["toss_krw_fmt"] != "수동 관리"
```

If the test file uses a different public function than `build_kr_morning_report_payload`, place the same assertions on the existing payload-returning helper used by nearby tests.

- [ ] **Step 2: Run morning report test to verify failure**

Run:

```bash
uv run pytest tests/test_n8n_kr_morning_report.py::test_kr_morning_report_includes_toss_api_cash -q
```

Expected: FAIL because Toss cash is hard-coded as `None` / `"수동 관리"`.

- [ ] **Step 3: Implement Toss cash helper**

Modify imports in `app/services/n8n_kr_morning_report_service.py`:

```python
from app.core.config import settings
from app.services.toss_portfolio_service import fetch_toss_portfolio_snapshot
```

Add helper near the cash code:

```python
async def _fetch_toss_cash_balance() -> tuple[float | None, float | None, list[dict[str, str]]]:
    if not bool(getattr(settings, "toss_api_enabled", False)):
        return None, None, []
    try:
        snapshot = await fetch_toss_portfolio_snapshot()
        return (
            float(snapshot.cash_krw) if snapshot.cash_krw is not None else None,
            float(snapshot.cash_usd) if snapshot.cash_usd is not None else None,
            [
                {"source": str(item.get("source", "toss_api")), "error": str(item.get("error", ""))}
                for item in snapshot.errors
            ],
        )
    except Exception as exc:
        return None, None, [{"source": "toss_api", "error": str(exc)}]
```

In the payload build function, call it alongside KIS cash:

```python
    toss_cash_krw, toss_cash_usd, toss_cash_errors = await _fetch_toss_cash_balance()
    errors.extend(toss_cash_errors)
```

Replace the current `cash_balance` dict with:

```python
    toss_krw_for_total = toss_cash_krw or 0.0
    cash_balance = {
        "kis_krw": kis_cash,
        "kis_krw_fmt": fmt_amount(kis_cash),
        "toss_krw": toss_cash_krw,
        "toss_krw_fmt": fmt_amount(toss_cash_krw)
        if toss_cash_krw is not None
        else "API 미사용" if not bool(getattr(settings, "toss_api_enabled", False))
        else "조회 실패",
        "toss_usd": toss_cash_usd,
        "toss_usd_fmt": f"{toss_cash_usd:,.2f} USD"
        if toss_cash_usd is not None
        else None,
        "total_krw": kis_cash + toss_krw_for_total,
        "total_krw_fmt": fmt_amount(kis_cash + toss_krw_for_total),
    }
```

- [ ] **Step 4: Run morning report test to verify pass**

Run:

```bash
uv run pytest tests/test_n8n_kr_morning_report.py::test_kr_morning_report_includes_toss_api_cash -q
```

Expected: PASS.

- [ ] **Step 5: Run morning report suite**

Run:

```bash
uv run pytest tests/test_n8n_kr_morning_report.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit morning report work**

Run:

```bash
git add app/services/n8n_kr_morning_report_service.py tests/test_n8n_kr_morning_report.py
git commit -m "feat(ROB-532): show Toss API cash in morning report"
```

## Task 8: Update MCP README

**Files:**
- Modify: `app/mcp_server/README.md`

- [ ] **Step 1: Update `get_cash_balance` docs**

In `app/mcp_server/README.md`, under `get_cash_balance` broker-specific contract, add:

```markdown
- **Toss (`account="toss"`, only when `TOSS_API_ENABLED=true`)**
  - `balance`: Toss buying power for the row currency
  - `orderable`: `0.0`; Toss portfolio integration is read-only in ROB-532, while order mutation tools are delivered separately
  - Emits one KRW row when KRW buying power is available and one USD row when USD buying power is available
  - If `account="toss"` is requested and the Toss API read fails, the tool fails closed; in all-account mode it records a partial `toss_api` error
```

- [ ] **Step 2: Update `get_holdings` docs**

Under `get_holdings` response contract additions, add:

```markdown
- When `TOSS_API_ENABLED=true`, Toss Open API holdings are emitted with `broker="toss"`, `source="toss_api"`, and `order_routable=false` until Toss live-order tools exist. The per-position `sellable_quantity` field comes from Toss `/api/v1/sellable-quantity` and is informational in ROB-532.
- When Toss API holdings succeed, duplicate Toss `manual_holdings` rows for the same market/symbol are hidden from normal output. KIS and Toss holdings for the same symbol are not deduplicated because they are separate broker subaccounts.
- When Toss API holdings fail, existing Toss `manual_holdings` rows remain visible as fallback and the response includes a partial `source="toss_api"` error.
```

- [ ] **Step 3: Commit docs**

Run:

```bash
git add app/mcp_server/README.md
git commit -m "docs(ROB-532): document Toss API portfolio surfaces"
```

## Task 9: Full Verification

**Files:**
- No file edits in this task.

- [ ] **Step 1: Run focused pytest suite**

Run:

```bash
uv run pytest \
  tests/test_toss_portfolio_service.py \
  tests/test_mcp_portfolio_tools.py \
  tests/test_order_sell_routability_message.py \
  tests/test_invest_home_readers.py \
  tests/test_invest_home_service.py \
  tests/test_n8n_kr_morning_report.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run ROB-530 Toss foundation regression**

Run:

```bash
uv run pytest tests/services/brokers/toss -q
```

Expected: PASS.

- [ ] **Step 3: Run lint**

Run:

```bash
make lint
```

Expected: PASS.

- [ ] **Step 4: Run type check if available in this branch**

Run:

```bash
uv run ty check app tests
```

Expected: PASS, or a known unrelated ty baseline. If unrelated baseline failures appear, paste the first failing file and error in the PR notes.

- [ ] **Step 5: Inspect migration status**

Run:

```bash
git diff --name-only -- alembic app/models
```

Expected: no Alembic revision files and no ORM model changes. ROB-532 acceptance criteria require migration 0.

- [ ] **Step 6: Final git diff review**

Run:

```bash
git diff --stat
git diff -- app/services/toss_portfolio_service.py app/mcp_server/tooling/portfolio_holdings.py app/mcp_server/tooling/portfolio_cash.py app/services/invest_home_readers.py app/services/invest_home_service.py
```

Expected: changes are limited to read-only portfolio surfaces, tests, and docs. There must be no Toss order mutation implementation in this diff.

## Self-Review Checklist

- ROB-532 flag-off behavior is covered by existing manual holdings tests and must remain unchanged.
- Flag-on MCP holdings emits Toss API rows with `order_routable=false` until Toss live-order tools exist.
- Flag-on MCP holdings hides only Toss manual duplicate rows, not KIS rows.
- Flag-on cash includes Toss KRW and USD buying power as balances, but `orderable=0.0` keeps them out of available-capital order sizing.
- Invest Home accepts `toss_api` and marks it live/source-of-truth, but not tradeable or sellable until Toss live-order tools exist.
- Morning report no longer shows `"수동 관리"` for Toss cash when the API is enabled and returns cash.
- No migration.
- No order mutation.
- PR/Linear labels include `high_risk_change`, `needs_stronger_model_review`, and `hold_for_final_review`.

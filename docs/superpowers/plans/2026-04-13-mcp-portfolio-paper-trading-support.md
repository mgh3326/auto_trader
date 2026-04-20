# MCP Portfolio Paper Trading Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `get_holdings`, `get_position`, `get_cash_balance`, and `get_available_capital` MCP tools to query paper trading accounts alongside live brokerage accounts, using an `account="paper"` / `account="paper:<name>"` selector pattern.

**Architecture:**
Add a new isolated handler module `app/mcp_server/tooling/paper_portfolio_handler.py` that (a) parses the `paper[:<name>]` account token, (b) resolves it to one or more `PaperAccount` rows via `PaperTradingService`, and (c) translates paper positions/cash into the canonical shapes emitted by the existing live-broker collectors. Existing real-portfolio code paths remain byte-for-byte unchanged: each real impl (`_collect_portfolio_positions`, `_get_position_impl`, `get_cash_balance_impl`, `get_available_capital_impl`) short-circuits to the paper handler when the account filter starts with `paper`.

**Tech Stack:** Python 3.13, SQLAlchemy async, FastMCP, pytest-asyncio, `PaperTradingService` (`app/services/paper_trading_service.py`), `StockInfoService` (`app/services/stock_info_service.py`), `get_upbit_korean_name_by_coin` (`app/services/upbit_symbol_universe_service.py`).

---

## File Structure

**Create:**
- `app/mcp_server/tooling/paper_portfolio_handler.py` — all paper-specific logic (account parsing, position collection, cash collection, symbol→name resolution).
- `tests/test_paper_portfolio_handler.py` — unit tests for the handler in isolation.

**Modify:**
- `app/mcp_server/tooling/portfolio_holdings.py` — (1) `_collect_portfolio_positions` delegates to paper handler when account filter is `paper*`; (2) `_get_position_impl` gains `account_type` + `paper_account` parameters; (3) `get_position` and `get_holdings` tool registrations forward the new parameters / docstrings.
- `app/mcp_server/tooling/portfolio_cash.py` — `get_cash_balance_impl` and `get_available_capital_impl` delegate to paper handler when account filter is `paper*`.
- `tests/test_mcp_portfolio_tools.py` — integration coverage exercising the tools through `build_tools()` with paper accounts.

**Do not touch (but read as reference):**
- `app/mcp_server/tooling/shared.py` — reuse `normalize_position_symbol`, `position_to_output`, `recalculate_profit_fields`, `parse_holdings_market_filter`, `is_position_symbol_match`.
- `app/services/paper_trading_service.py` — reuse `list_accounts`, `get_account_by_name`, `get_positions`, `get_cash_balance`. No changes to this service.

---

## Output-shape contract (important)

The paper handler MUST emit position dicts that match the canonical shape used by `_collect_portfolio_positions` so that downstream filtering (`_match_account_filter`, market filter), price fill (`_fetch_price_map_for_positions`), and formatting (`position_to_output`) work unchanged.

Canonical per-position keys (see `_collect_kis_positions` in `portfolio_holdings.py:247-268` for the reference shape):
```
account            # "paper:<account_name>"
account_name       # PaperAccount.name
broker             # "paper"
source             # "paper"
instrument_type    # "equity_kr" | "equity_us" | "crypto"
market             # "kr" | "us" | "crypto"  (via _INSTRUMENT_TO_MARKET)
symbol             # normalized DB form
name               # resolved via StockInfoService / upbit universe lookup
quantity           # float
avg_buy_price      # float   (mapped from PaperPosition.avg_price)
current_price      # float | None
evaluation_amount  # float | None
profit_loss        # float | None
profit_rate        # float | None
```

Note: the prompt's example literal (`avg_price`, `eval_amount`) is shown for intent. The authoritative contract is "반환 형식을 기존 get_holdings 출력과 동일하게 맞춤" in the spec — so we use `avg_buy_price` / `evaluation_amount` to match `shared.position_to_output`.

---

### Task 1: Paper handler skeleton + account-token parser

**Files:**
- Create: `app/mcp_server/tooling/paper_portfolio_handler.py`
- Test: `tests/test_paper_portfolio_handler.py`

- [ ] **Step 1: Write the failing tests for `parse_paper_account_token`**

```python
# tests/test_paper_portfolio_handler.py
"""Unit tests for paper portfolio handler."""

from __future__ import annotations

import pytest

from app.mcp_server.tooling.paper_portfolio_handler import (
    PaperAccountSelector,
    is_paper_account_token,
    parse_paper_account_token,
)


class TestIsPaperAccountToken:
    def test_exact_paper(self):
        assert is_paper_account_token("paper") is True

    def test_paper_with_name(self):
        assert is_paper_account_token("paper:데이트레이딩") is True

    def test_case_insensitive(self):
        assert is_paper_account_token("PAPER") is True
        assert is_paper_account_token("Paper:swing") is True

    def test_paper_with_whitespace(self):
        assert is_paper_account_token("  paper  ") is True

    def test_non_paper(self):
        assert is_paper_account_token("kis") is False
        assert is_paper_account_token("upbit") is False
        assert is_paper_account_token("paperless") is False  # prefix-only match forbidden
        assert is_paper_account_token(None) is False
        assert is_paper_account_token("") is False


class TestParsePaperAccountToken:
    def test_bare_paper_returns_all_selector(self):
        sel = parse_paper_account_token("paper")
        assert sel == PaperAccountSelector(account_name=None)

    def test_paper_with_name(self):
        sel = parse_paper_account_token("paper:데이트레이딩")
        assert sel == PaperAccountSelector(account_name="데이트레이딩")

    def test_trims_whitespace(self):
        sel = parse_paper_account_token("  paper :   swing  ")
        assert sel == PaperAccountSelector(account_name="swing")

    def test_empty_name_after_colon(self):
        sel = parse_paper_account_token("paper:")
        assert sel == PaperAccountSelector(account_name=None)

    def test_non_paper_raises(self):
        with pytest.raises(ValueError, match="not a paper account token"):
            parse_paper_account_token("kis")
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `uv run pytest tests/test_paper_portfolio_handler.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.mcp_server.tooling.paper_portfolio_handler'`

- [ ] **Step 3: Implement the parser**

```python
# app/mcp_server/tooling/paper_portfolio_handler.py
"""Paper trading portfolio handler for MCP tools.

Keeps paper-specific collection/translation logic isolated so that the live
broker tooling files (portfolio_holdings.py, portfolio_cash.py) only need a
single delegation point.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PaperAccountSelector:
    """Resolved selector for paper account queries.

    account_name is None when the caller passed the bare "paper" token, which
    means "all active paper accounts".
    """

    account_name: str | None


def _strip(value: str | None) -> str:
    return (value or "").strip()


def is_paper_account_token(account: str | None) -> bool:
    token = _strip(account).lower()
    if not token:
        return False
    if token == "paper":
        return True
    # Prefix-only match would match "paperless"; require explicit ":" delimiter.
    return token.startswith("paper:")


def parse_paper_account_token(account: str | None) -> PaperAccountSelector:
    token = _strip(account)
    if not is_paper_account_token(token):
        raise ValueError(f"not a paper account token: {account!r}")

    lowered = token.lower()
    if lowered == "paper":
        return PaperAccountSelector(account_name=None)

    # Split on the first ":" only; preserve case of the account name.
    _, _, raw_name = token.partition(":")
    name = raw_name.strip()
    return PaperAccountSelector(account_name=name or None)


__all__ = [
    "PaperAccountSelector",
    "is_paper_account_token",
    "parse_paper_account_token",
]
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `uv run pytest tests/test_paper_portfolio_handler.py -v`
Expected: PASS — all 9 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/paper_portfolio_handler.py tests/test_paper_portfolio_handler.py
git commit -m "feat(mcp): add paper account token parser for portfolio tools"
```

---

### Task 2: Symbol→name resolver for paper positions

**Files:**
- Modify: `app/mcp_server/tooling/paper_portfolio_handler.py`
- Modify: `tests/test_paper_portfolio_handler.py`

Paper positions store only `symbol` + `instrument_type`. The canonical holdings output requires a human-readable `name`. This task adds `resolve_paper_position_name(symbol, instrument_type, db)` which looks up names per market.

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_paper_portfolio_handler.py

from unittest.mock import AsyncMock

from app.mcp_server.tooling.paper_portfolio_handler import (
    resolve_paper_position_name,
)


class TestResolvePaperPositionName:
    @pytest.mark.asyncio
    async def test_equity_kr_uses_stock_info(self, monkeypatch):
        fake_stock = type("S", (), {"name": "삼성전자"})()
        async def fake_get(self, symbol):
            assert symbol == "005930"
            return fake_stock
        monkeypatch.setattr(
            "app.services.stock_info_service.StockInfoService.get_stock_info_by_symbol",
            fake_get,
        )
        name = await resolve_paper_position_name("005930", "equity_kr", db=AsyncMock())
        assert name == "삼성전자"

    @pytest.mark.asyncio
    async def test_equity_us_uses_stock_info(self, monkeypatch):
        fake_stock = type("S", (), {"name": "Apple Inc."})()
        async def fake_get(self, symbol):
            return fake_stock
        monkeypatch.setattr(
            "app.services.stock_info_service.StockInfoService.get_stock_info_by_symbol",
            fake_get,
        )
        name = await resolve_paper_position_name("AAPL", "equity_us", db=AsyncMock())
        assert name == "Apple Inc."

    @pytest.mark.asyncio
    async def test_stock_info_missing_falls_back_to_symbol(self, monkeypatch):
        async def fake_get(self, symbol):
            return None
        monkeypatch.setattr(
            "app.services.stock_info_service.StockInfoService.get_stock_info_by_symbol",
            fake_get,
        )
        name = await resolve_paper_position_name("NEWCO", "equity_us", db=AsyncMock())
        assert name == "NEWCO"

    @pytest.mark.asyncio
    async def test_crypto_uses_upbit_universe(self, monkeypatch):
        monkeypatch.setattr(
            "app.mcp_server.tooling.paper_portfolio_handler."
            "get_upbit_korean_name_by_coin",
            AsyncMock(return_value="비트코인"),
        )
        name = await resolve_paper_position_name("KRW-BTC", "crypto", db=AsyncMock())
        assert name == "비트코인"

    @pytest.mark.asyncio
    async def test_crypto_lookup_failure_falls_back_to_symbol(self, monkeypatch):
        from app.services.upbit_symbol_universe_service import (
            UpbitSymbolNotRegisteredError,
        )
        async def boom(coin, quote_currency=None):
            raise UpbitSymbolNotRegisteredError("x")
        monkeypatch.setattr(
            "app.mcp_server.tooling.paper_portfolio_handler."
            "get_upbit_korean_name_by_coin",
            boom,
        )
        name = await resolve_paper_position_name("KRW-XYZ", "crypto", db=AsyncMock())
        assert name == "KRW-XYZ"
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `uv run pytest tests/test_paper_portfolio_handler.py::TestResolvePaperPositionName -v`
Expected: FAIL with `ImportError` for `resolve_paper_position_name`.

- [ ] **Step 3: Implement the resolver**

Append to `app/mcp_server/tooling/paper_portfolio_handler.py`:

```python
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.stock_info_service import StockInfoService
from app.services.upbit_symbol_universe_service import (
    UpbitSymbolInactiveError,
    UpbitSymbolNotRegisteredError,
    UpbitSymbolUniverseLookupError,
    get_upbit_korean_name_by_coin,
)

logger = logging.getLogger(__name__)


async def resolve_paper_position_name(
    symbol: str,
    instrument_type: str,
    *,
    db: AsyncSession,
) -> str:
    """Resolve a human-readable name for a paper position.

    Falls back to ``symbol`` when lookup fails or the symbol is unknown, so
    callers always receive a non-empty string.
    """
    if instrument_type in ("equity_kr", "equity_us"):
        try:
            service = StockInfoService(db)
            info = await service.get_stock_info_by_symbol(symbol)
            if info is not None and info.name:
                return str(info.name)
        except Exception as exc:
            logger.debug("Failed to resolve stock_info name for %s: %s", symbol, exc)
        return symbol

    if instrument_type == "crypto":
        # symbol is in "KRW-BTC" form; extract quote currency + coin
        quote, _, coin = symbol.partition("-")
        if not coin:
            return symbol
        try:
            return await get_upbit_korean_name_by_coin(
                coin, quote_currency=quote or "KRW"
            )
        except (
            UpbitSymbolNotRegisteredError,
            UpbitSymbolInactiveError,
            UpbitSymbolUniverseLookupError,
        ):
            return symbol
        except Exception as exc:
            logger.debug("Failed to resolve upbit name for %s: %s", symbol, exc)
            return symbol

    return symbol
```

Also extend `__all__` with `"resolve_paper_position_name"`.

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `uv run pytest tests/test_paper_portfolio_handler.py -v`
Expected: PASS — all existing tests plus the 5 new resolver tests.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/paper_portfolio_handler.py tests/test_paper_portfolio_handler.py
git commit -m "feat(mcp): add paper position name resolver (stock_info + upbit)"
```

---

### Task 3: `collect_paper_positions` — translate paper positions into canonical shape

**Files:**
- Modify: `app/mcp_server/tooling/paper_portfolio_handler.py`
- Modify: `tests/test_paper_portfolio_handler.py`

Produce a list of position dicts in the canonical shape described in the "Output-shape contract" section above, for either one named paper account or all active paper accounts.

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_paper_portfolio_handler.py
from decimal import Decimal

from app.mcp_server.tooling.paper_portfolio_handler import collect_paper_positions


class _FakePaperAccount:
    def __init__(self, id_: int, name: str, is_active: bool = True):
        self.id = id_
        self.name = name
        self.is_active = is_active


class _FakePaperService:
    """Drop-in replacement for PaperTradingService in tests."""

    def __init__(
        self,
        *,
        accounts: list[_FakePaperAccount],
        positions_by_account: dict[int, list[dict]],
        cash_by_account: dict[int, dict[str, Decimal]] | None = None,
    ):
        self._accounts = accounts
        self._positions = positions_by_account
        self._cash = cash_by_account or {}

    async def list_accounts(self, is_active=True):
        if is_active is None:
            return list(self._accounts)
        return [a for a in self._accounts if a.is_active == is_active]

    async def get_account_by_name(self, name):
        for a in self._accounts:
            if a.name == name:
                return a
        return None

    async def get_positions(self, account_id, market=None):
        return list(self._positions.get(account_id, []))

    async def get_cash_balance(self, account_id):
        return self._cash.get(account_id, {"krw": Decimal("0"), "usd": Decimal("0")})


@pytest.mark.asyncio
async def test_collect_paper_positions_all_active(monkeypatch):
    svc = _FakePaperService(
        accounts=[
            _FakePaperAccount(1, "default"),
            _FakePaperAccount(2, "데이트레이딩"),
        ],
        positions_by_account={
            1: [
                {
                    "symbol": "005930",
                    "instrument_type": "equity_kr",
                    "quantity": Decimal("10"),
                    "avg_price": Decimal("72000"),
                    "total_invested": Decimal("720000"),
                    "current_price": Decimal("73500"),
                    "evaluation_amount": Decimal("735000"),
                    "unrealized_pnl": Decimal("15000"),
                    "pnl_pct": Decimal("2.08"),
                }
            ],
            2: [],
        },
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.paper_portfolio_handler._build_service",
        lambda db: svc,
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.paper_portfolio_handler.resolve_paper_position_name",
        AsyncMock(return_value="삼성전자"),
    )

    positions, errors = await collect_paper_positions(
        selector=PaperAccountSelector(account_name=None),
        market_filter=None,
    )

    assert errors == []
    assert len(positions) == 1
    p = positions[0]
    assert p["account"] == "paper:default"
    assert p["account_name"] == "default"
    assert p["broker"] == "paper"
    assert p["source"] == "paper"
    assert p["instrument_type"] == "equity_kr"
    assert p["market"] == "kr"
    assert p["symbol"] == "005930"
    assert p["name"] == "삼성전자"
    assert p["quantity"] == 10.0
    assert p["avg_buy_price"] == 72000.0
    assert p["current_price"] == 73500.0
    assert p["evaluation_amount"] == 735000.0
    assert p["profit_loss"] == 15000.0
    assert p["profit_rate"] == pytest.approx(2.08)


@pytest.mark.asyncio
async def test_collect_paper_positions_named_account(monkeypatch):
    svc = _FakePaperService(
        accounts=[
            _FakePaperAccount(1, "default"),
            _FakePaperAccount(2, "데이트레이딩"),
        ],
        positions_by_account={
            1: [{"symbol": "AAPL", "instrument_type": "equity_us",
                 "quantity": Decimal("1"), "avg_price": Decimal("100"),
                 "total_invested": Decimal("100"), "current_price": None,
                 "evaluation_amount": None, "unrealized_pnl": None,
                 "pnl_pct": None}],
            2: [{"symbol": "KRW-BTC", "instrument_type": "crypto",
                 "quantity": Decimal("0.5"), "avg_price": Decimal("50000000"),
                 "total_invested": Decimal("25000000"), "current_price": None,
                 "evaluation_amount": None, "unrealized_pnl": None,
                 "pnl_pct": None}],
        },
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.paper_portfolio_handler._build_service",
        lambda db: svc,
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.paper_portfolio_handler.resolve_paper_position_name",
        AsyncMock(return_value="비트코인"),
    )

    positions, errors = await collect_paper_positions(
        selector=PaperAccountSelector(account_name="데이트레이딩"),
        market_filter=None,
    )

    assert errors == []
    assert len(positions) == 1
    assert positions[0]["account"] == "paper:데이트레이딩"
    assert positions[0]["symbol"] == "KRW-BTC"
    assert positions[0]["market"] == "crypto"


@pytest.mark.asyncio
async def test_collect_paper_positions_missing_account_returns_error(monkeypatch):
    svc = _FakePaperService(accounts=[], positions_by_account={})
    monkeypatch.setattr(
        "app.mcp_server.tooling.paper_portfolio_handler._build_service",
        lambda db: svc,
    )

    positions, errors = await collect_paper_positions(
        selector=PaperAccountSelector(account_name="ghost"),
        market_filter=None,
    )

    assert positions == []
    assert len(errors) == 1
    assert errors[0]["source"] == "paper"
    assert "ghost" in errors[0]["error"]


@pytest.mark.asyncio
async def test_collect_paper_positions_applies_market_filter(monkeypatch):
    svc = _FakePaperService(
        accounts=[_FakePaperAccount(1, "default")],
        positions_by_account={
            1: [
                {"symbol": "005930", "instrument_type": "equity_kr",
                 "quantity": Decimal("1"), "avg_price": Decimal("70000"),
                 "total_invested": Decimal("70000"), "current_price": None,
                 "evaluation_amount": None, "unrealized_pnl": None,
                 "pnl_pct": None},
                {"symbol": "AAPL", "instrument_type": "equity_us",
                 "quantity": Decimal("1"), "avg_price": Decimal("100"),
                 "total_invested": Decimal("100"), "current_price": None,
                 "evaluation_amount": None, "unrealized_pnl": None,
                 "pnl_pct": None},
            ],
        },
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.paper_portfolio_handler._build_service",
        lambda db: svc,
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.paper_portfolio_handler.resolve_paper_position_name",
        AsyncMock(return_value="name"),
    )

    positions, errors = await collect_paper_positions(
        selector=PaperAccountSelector(account_name=None),
        market_filter="equity_us",
    )

    assert errors == []
    assert [p["symbol"] for p in positions] == ["AAPL"]
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `uv run pytest tests/test_paper_portfolio_handler.py -v`
Expected: FAIL — `ImportError: cannot import name 'collect_paper_positions'`.

- [ ] **Step 3: Implement `collect_paper_positions`**

Append to `app/mcp_server/tooling/paper_portfolio_handler.py`:

```python
from typing import Any

from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.shared import (
    INSTRUMENT_TO_MARKET as _INSTRUMENT_TO_MARKET,
)
from app.mcp_server.tooling.shared import (
    normalize_position_symbol as _normalize_position_symbol,
)
from app.mcp_server.tooling.shared import (
    to_float as _to_float,
)
from app.mcp_server.tooling.shared import (
    to_optional_float as _to_optional_float,
)
from app.services.paper_trading_service import PaperTradingService


def _build_service(db: AsyncSession) -> PaperTradingService:
    """Construction seam so tests can swap in a fake service."""
    return PaperTradingService(db)


async def _resolve_target_accounts(
    service: PaperTradingService,
    selector: PaperAccountSelector,
) -> tuple[list[Any], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []

    if selector.account_name is None:
        accounts = await service.list_accounts(is_active=True)
        return list(accounts), errors

    account = await service.get_account_by_name(selector.account_name)
    if account is None or not account.is_active:
        errors.append(
            {
                "source": "paper",
                "error": f"paper account not found: {selector.account_name}",
            }
        )
        return [], errors
    return [account], errors


def _paper_position_to_canonical(
    *,
    account_name: str,
    raw_position: dict[str, Any],
    display_name: str,
) -> dict[str, Any]:
    instrument_type = str(raw_position["instrument_type"])
    symbol = _normalize_position_symbol(str(raw_position["symbol"]), instrument_type)

    return {
        "account": f"paper:{account_name}",
        "account_name": account_name,
        "broker": "paper",
        "source": "paper",
        "instrument_type": instrument_type,
        "market": _INSTRUMENT_TO_MARKET.get(instrument_type, instrument_type),
        "symbol": symbol,
        "name": display_name or symbol,
        "quantity": _to_float(raw_position.get("quantity")),
        "avg_buy_price": _to_float(raw_position.get("avg_price")),
        "current_price": _to_optional_float(raw_position.get("current_price")),
        "evaluation_amount": _to_optional_float(raw_position.get("evaluation_amount")),
        "profit_loss": _to_optional_float(raw_position.get("unrealized_pnl")),
        "profit_rate": _to_optional_float(raw_position.get("pnl_pct")),
    }


async def collect_paper_positions(
    *,
    selector: PaperAccountSelector,
    market_filter: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect paper positions in the canonical portfolio shape.

    Parameters
    ----------
    selector
        PaperAccountSelector from ``parse_paper_account_token``.
    market_filter
        One of ``equity_kr`` / ``equity_us`` / ``crypto`` / ``None`` (all).
    """
    positions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    async with AsyncSessionLocal() as db:
        service = _build_service(db)

        target_accounts, lookup_errors = await _resolve_target_accounts(
            service, selector
        )
        errors.extend(lookup_errors)

        for account in target_accounts:
            try:
                raw_positions = await service.get_positions(
                    account_id=account.id, market=market_filter
                )
            except Exception as exc:
                errors.append(
                    {
                        "source": "paper",
                        "account": f"paper:{account.name}",
                        "error": str(exc),
                    }
                )
                continue

            for raw in raw_positions:
                try:
                    display_name = await resolve_paper_position_name(
                        str(raw["symbol"]),
                        str(raw["instrument_type"]),
                        db=db,
                    )
                except Exception as exc:
                    logger.debug(
                        "name resolution failed for paper %s: %s", raw["symbol"], exc
                    )
                    display_name = str(raw["symbol"])
                positions.append(
                    _paper_position_to_canonical(
                        account_name=account.name,
                        raw_position=raw,
                        display_name=display_name,
                    )
                )

    return positions, errors
```

Extend `__all__` with `"collect_paper_positions"` and `"_build_service"` (the latter because tests patch it).

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `uv run pytest tests/test_paper_portfolio_handler.py -v`
Expected: PASS — all previous tests plus the 4 new `collect_paper_positions` tests.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/paper_portfolio_handler.py tests/test_paper_portfolio_handler.py
git commit -m "feat(mcp): translate paper positions into canonical portfolio shape"
```

---

### Task 4: `get_holdings` — route `paper*` account filter to paper handler

**Files:**
- Modify: `app/mcp_server/tooling/portfolio_holdings.py:605-694` (inside `_collect_portfolio_positions`) and the `get_holdings` registration block at `portfolio_holdings.py:1004-1030`.
- Modify: `tests/test_mcp_portfolio_tools.py` (add a new test).

Key idea: when `account` starts with `paper`, bypass the live-broker gatherers entirely, call `collect_paper_positions`, then reuse the existing `include_current_price` price-fill loop (`_fetch_price_map_for_positions`) so the returned list honors the same profit-recalc logic.

- [ ] **Step 1: Write the failing integration test**

```python
# Append to tests/test_mcp_portfolio_tools.py
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling import paper_portfolio_handler
from tests._mcp_tooling_support import build_tools


class _StubAcc:
    def __init__(self, id_, name, is_active=True):
        self.id, self.name, self.is_active = id_, name, is_active


class _StubPaperService:
    def __init__(self, accounts, positions, cash=None):
        self._a, self._p, self._c = accounts, positions, cash or {}

    async def list_accounts(self, is_active=True):
        return [a for a in self._a if (is_active is None or a.is_active == is_active)]

    async def get_account_by_name(self, name):
        return next((a for a in self._a if a.name == name), None)

    async def get_positions(self, account_id, market=None):
        return self._p.get(account_id, [])

    async def get_cash_balance(self, account_id):
        return self._c.get(account_id, {"krw": Decimal("0"), "usd": Decimal("0")})


@pytest.mark.asyncio
async def test_get_holdings_with_paper_account_filter(monkeypatch):
    tools = build_tools()

    svc = _StubPaperService(
        accounts=[_StubAcc(1, "default")],
        positions=[
            {
                "symbol": "005930",
                "instrument_type": "equity_kr",
                "quantity": Decimal("10"),
                "avg_price": Decimal("72000"),
                "total_invested": Decimal("720000"),
                "current_price": Decimal("73500"),
                "evaluation_amount": Decimal("735000"),
                "unrealized_pnl": Decimal("15000"),
                "pnl_pct": Decimal("2.08"),
            },
        ],
    )
    svc._p = {1: svc._p} if isinstance(svc._p, list) else {1: svc._p}  # noqa: SLF001
    # Cleaner: rebuild with dict
    svc = _StubPaperService(
        accounts=[_StubAcc(1, "default")],
        positions={
            1: [
                {
                    "symbol": "005930",
                    "instrument_type": "equity_kr",
                    "quantity": Decimal("10"),
                    "avg_price": Decimal("72000"),
                    "total_invested": Decimal("720000"),
                    "current_price": Decimal("73500"),
                    "evaluation_amount": Decimal("735000"),
                    "unrealized_pnl": Decimal("15000"),
                    "pnl_pct": Decimal("2.08"),
                }
            ]
        },
    )
    monkeypatch.setattr(paper_portfolio_handler, "_build_service", lambda db: svc)
    monkeypatch.setattr(
        paper_portfolio_handler,
        "resolve_paper_position_name",
        AsyncMock(return_value="삼성전자"),
    )
    # Avoid real live-broker calls leaking in if the guard regresses
    monkeypatch.setattr(
        "app.mcp_server.tooling.portfolio_holdings._collect_kis_positions",
        AsyncMock(side_effect=AssertionError("KIS must not be called for paper")),
    )

    result = await tools["get_holdings"](account="paper", include_current_price=False)

    assert result["total_positions"] == 1
    assert result["accounts"][0]["account"] == "paper:default"
    pos = result["accounts"][0]["positions"][0]
    assert pos["symbol"] == "005930"
    assert pos["name"] == "삼성전자"
    assert pos["quantity"] == 10.0
    assert pos["avg_buy_price"] == 72000.0


@pytest.mark.asyncio
async def test_get_holdings_with_named_paper_account(monkeypatch):
    tools = build_tools()
    svc = _StubPaperService(
        accounts=[_StubAcc(1, "default"), _StubAcc(2, "데이트레이딩")],
        positions={
            1: [],
            2: [
                {
                    "symbol": "AAPL",
                    "instrument_type": "equity_us",
                    "quantity": Decimal("5"),
                    "avg_price": Decimal("150"),
                    "total_invested": Decimal("750"),
                    "current_price": None,
                    "evaluation_amount": None,
                    "unrealized_pnl": None,
                    "pnl_pct": None,
                }
            ],
        },
    )
    monkeypatch.setattr(paper_portfolio_handler, "_build_service", lambda db: svc)
    monkeypatch.setattr(
        paper_portfolio_handler,
        "resolve_paper_position_name",
        AsyncMock(return_value="Apple Inc."),
    )

    result = await tools["get_holdings"](
        account="paper:데이트레이딩", include_current_price=False
    )

    assert result["total_positions"] == 1
    assert result["accounts"][0]["account"] == "paper:데이트레이딩"
    assert result["accounts"][0]["positions"][0]["symbol"] == "AAPL"
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `uv run pytest tests/test_mcp_portfolio_tools.py::test_get_holdings_with_paper_account_filter tests/test_mcp_portfolio_tools.py::test_get_holdings_with_named_paper_account -v`
Expected: FAIL — positions list is empty because paper accounts are never collected; or the KIS assertion fires because the real gatherers are still running.

- [ ] **Step 3: Add paper branch to `_collect_portfolio_positions`**

Open `app/mcp_server/tooling/portfolio_holdings.py`. At the **top** of `_collect_portfolio_positions` (before `tasks: list[Any] = []` at line 616), insert:

```python
    # Short-circuit to paper handler when the caller asked for a paper account.
    from app.mcp_server.tooling.paper_portfolio_handler import (
        collect_paper_positions,
        is_paper_account_token,
        parse_paper_account_token,
    )

    market_filter = _parse_holdings_market_filter(market)
    if is_paper_account_token(account):
        selector = parse_paper_account_token(account)
        positions, errors = await collect_paper_positions(
            selector=selector,
            market_filter=market_filter,
        )

        if account_name:
            account_name_filter = account_name.strip().lower()
            positions = [
                p
                for p in positions
                if account_name_filter in str(p.get("account_name", "")).lower()
            ]

        if include_current_price and positions:
            price_map, price_errors, error_map = await _fetch_price_map_for_positions(
                positions
            )
            errors.extend(price_errors)
            for position in positions:
                key = (position["instrument_type"], position["symbol"])
                needs_refresh = _position_needs_current_price_refresh(position)
                price = price_map.get(key)
                if price is not None and needs_refresh:
                    position["current_price"] = price
                    _recalculate_profit_fields(position)
                elif (error := error_map.get(key)) is not None and needs_refresh:
                    position["price_error"] = error
        else:
            for position in positions:
                position["current_price"] = None
                position["evaluation_amount"] = None
                position["profit_loss"] = None
                position["profit_rate"] = None

        positions.sort(
            key=lambda p: (p["account"], p["market"], p["symbol"])
        )
        return positions, errors, market_filter, account

    account_filter = _normalize_account_filter(account)
```

Then **remove** the now-duplicated line `market_filter = _parse_holdings_market_filter(market)` at the original location (line 613) — the code above now computes it first. Also remove the original `account_filter = _normalize_account_filter(account)` line (line 614) since it's now set on the non-paper branch only.

The non-paper branch below the new block is unchanged.

- [ ] **Step 4: Update `get_holdings` tool description**

Replace the `description=(...)` string on the `@mcp.tool(name="get_holdings", ...)` decorator at `portfolio_holdings.py:1005-1015` to mention paper:

```python
    @mcp.tool(
        name="get_holdings",
        description=(
            "Get holdings grouped by account. Supports account filter "
            "(kis/upbit/toss/samsung_pension/isa/paper/paper:<이름>) and market "
            "filter (kr/us/crypto). Cash balances are excluded. minimum_value "
            "filters out low-value positions when include_current_price=True. "
            "When minimum_value is None (default), per-currency thresholds are "
            "applied: KRW=5000, USD=10. Explicit number uses uniform threshold. "
            "Response includes filtered_count, filter_reason, and per-symbol "
            "price lookup errors."
        ),
    )
```

- [ ] **Step 5: Run the tests to confirm they pass**

Run: `uv run pytest tests/test_mcp_portfolio_tools.py -v -k "paper"` and then the full portfolio test file:
`uv run pytest tests/test_mcp_portfolio_tools.py tests/test_paper_portfolio_handler.py -v`
Expected: PASS — new paper tests pass AND all existing tests remain green.

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/portfolio_holdings.py tests/test_mcp_portfolio_tools.py
git commit -m "feat(mcp): support paper/paper:<name> in get_holdings account filter"
```

---

### Task 5: `get_position` — add `account_type` / `paper_account` parameters

**Files:**
- Modify: `app/mcp_server/tooling/portfolio_holdings.py:892-964` (`_get_position_impl`) and the `get_position` tool registration at lines 1032-1043.
- Modify: `tests/test_mcp_portfolio_tools.py`

`_get_position_impl` currently calls `_collect_portfolio_positions(account=None, ...)` and scans all live accounts. When `account_type="paper"`, we need it to query *only* paper accounts via the paper handler.

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_mcp_portfolio_tools.py
@pytest.mark.asyncio
async def test_get_position_paper_hit(monkeypatch):
    tools = build_tools()
    svc = _StubPaperService(
        accounts=[_StubAcc(1, "default")],
        positions={
            1: [
                {
                    "symbol": "005930",
                    "instrument_type": "equity_kr",
                    "quantity": Decimal("10"),
                    "avg_price": Decimal("72000"),
                    "total_invested": Decimal("720000"),
                    "current_price": Decimal("73500"),
                    "evaluation_amount": Decimal("735000"),
                    "unrealized_pnl": Decimal("15000"),
                    "pnl_pct": Decimal("2.08"),
                }
            ]
        },
    )
    monkeypatch.setattr(paper_portfolio_handler, "_build_service", lambda db: svc)
    monkeypatch.setattr(
        paper_portfolio_handler,
        "resolve_paper_position_name",
        AsyncMock(return_value="삼성전자"),
    )
    # Make live-broker gatherers explode if accidentally called
    monkeypatch.setattr(
        "app.mcp_server.tooling.portfolio_holdings._collect_kis_positions",
        AsyncMock(side_effect=AssertionError("live brokers must not be called")),
    )

    result = await tools["get_position"](
        symbol="005930", account_type="paper"
    )

    assert result["has_position"] is True
    assert result["accounts"] == ["paper:default"]
    assert result["positions"][0]["symbol"] == "005930"


@pytest.mark.asyncio
async def test_get_position_paper_named(monkeypatch):
    tools = build_tools()
    svc = _StubPaperService(
        accounts=[_StubAcc(1, "default"), _StubAcc(2, "데이트레이딩")],
        positions={
            1: [],
            2: [
                {"symbol": "005930", "instrument_type": "equity_kr",
                 "quantity": Decimal("5"), "avg_price": Decimal("70000"),
                 "total_invested": Decimal("350000"), "current_price": None,
                 "evaluation_amount": None, "unrealized_pnl": None,
                 "pnl_pct": None}
            ],
        },
    )
    monkeypatch.setattr(paper_portfolio_handler, "_build_service", lambda db: svc)
    monkeypatch.setattr(
        paper_portfolio_handler,
        "resolve_paper_position_name",
        AsyncMock(return_value="삼성전자"),
    )

    result = await tools["get_position"](
        symbol="005930", account_type="paper", paper_account="데이트레이딩"
    )

    assert result["has_position"] is True
    assert result["accounts"] == ["paper:데이트레이딩"]


@pytest.mark.asyncio
async def test_get_position_paper_miss(monkeypatch):
    tools = build_tools()
    svc = _StubPaperService(
        accounts=[_StubAcc(1, "default")],
        positions={1: []},
    )
    monkeypatch.setattr(paper_portfolio_handler, "_build_service", lambda db: svc)

    result = await tools["get_position"](symbol="005930", account_type="paper")

    assert result["has_position"] is False
    assert result["status"] == "미보유"


@pytest.mark.asyncio
async def test_get_position_invalid_account_type_raises(monkeypatch):
    tools = build_tools()
    with pytest.raises(ValueError, match="account_type must be"):
        await tools["get_position"](symbol="005930", account_type="bogus")
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `uv run pytest tests/test_mcp_portfolio_tools.py -v -k "paper and position"`
Expected: FAIL — `get_position` currently doesn't accept `account_type`.

- [ ] **Step 3: Modify `_get_position_impl` to accept and honor `account_type`**

Replace the existing `_get_position_impl` signature and body at `portfolio_holdings.py:892-964` with:

```python
async def _get_position_impl(
    *,
    symbol: str,
    market: str | None = None,
    account_type: str = "real",
    paper_account: str | None = None,
) -> dict[str, Any]:
    """Implementation for get_position tool.

    ``account_type``:
        - "real": existing behaviour — scan live brokerage + manual holdings.
        - "paper": scan paper trading accounts only. ``paper_account`` selects
          a specific named paper account; None means all active paper accounts.
    """
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    if account_type not in ("real", "paper"):
        raise ValueError("account_type must be 'real' or 'paper'")

    parsed_market = _parse_holdings_market_filter(market)
    if parsed_market == "equity_us":
        query_symbol = _normalize_position_symbol(symbol, "equity_us")
    elif parsed_market == "equity_kr":
        query_symbol = _normalize_position_symbol(symbol, "equity_kr")
    elif parsed_market == "crypto":
        query_symbol = _normalize_position_symbol(symbol, "crypto")
    else:
        query_symbol = symbol.strip().upper()

    if account_type == "paper":
        token = "paper" if not paper_account else f"paper:{paper_account}"
        positions, errors, _, _ = await _collect_portfolio_positions(
            account=token,
            market=market,
            include_current_price=True,
        )
    else:
        positions, errors, _, _ = await _collect_portfolio_positions(
            account=None,
            market=market,
            include_current_price=True,
        )

    matched_positions = [
        position
        for position in positions
        if _is_position_symbol_match(
            position_symbol=position["symbol"],
            query_symbol=query_symbol,
            instrument_type=position["instrument_type"],
        )
    ]

    if not matched_positions:
        return {
            "symbol": query_symbol,
            "market": _INSTRUMENT_TO_MARKET.get(parsed_market),
            "has_position": False,
            "status": "미보유",
            "position_count": 0,
            "positions": [],
            "errors": errors,
        }

    matched_positions.sort(
        key=lambda position: (
            position["account"],
            position["market"],
            position["symbol"],
        )
    )

    return {
        "symbol": query_symbol,
        "market": _INSTRUMENT_TO_MARKET.get(parsed_market),
        "has_position": True,
        "status": "보유",
        "position_count": len(matched_positions),
        "accounts": sorted({position["account"] for position in matched_positions}),
        "positions": [
            {
                "account": position["account"],
                "broker": position["broker"],
                "account_name": position["account_name"],
                **_position_to_output(position),
            }
            for position in matched_positions
        ],
        "errors": errors,
    }
```

Then update the tool registration at `portfolio_holdings.py:1032-1043`:

```python
    @mcp.tool(
        name="get_position",
        description=(
            "Check whether a symbol is currently held and return detailed "
            "positions across all accounts. account_type='real' (default) scans "
            "live brokerage and manual holdings; account_type='paper' scans "
            "paper trading accounts, optionally scoped by paper_account. "
            "Returns status='미보유' when no position exists."
        ),
    )
    async def get_position(
        symbol: str,
        market: str | None = None,
        account_type: str = "real",
        paper_account: str | None = None,
    ) -> dict[str, Any]:
        return await _get_position_impl(
            symbol=symbol,
            market=market,
            account_type=account_type,
            paper_account=paper_account,
        )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_mcp_portfolio_tools.py tests/test_paper_portfolio_handler.py -v`
Expected: PASS — all new paper `get_position` tests plus previous suites.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/portfolio_holdings.py tests/test_mcp_portfolio_tools.py
git commit -m "feat(mcp): add account_type/paper_account params to get_position"
```

---

### Task 6: `get_cash_balance` — route `paper*` to paper handler

**Files:**
- Modify: `app/mcp_server/tooling/paper_portfolio_handler.py` (add `collect_paper_cash_balances`).
- Modify: `app/mcp_server/tooling/portfolio_cash.py:95-222` (`get_cash_balance_impl`) and `app/mcp_server/tooling/portfolio_holdings.py:1086-1095` (`get_cash_balance` registration — update description).
- Modify: `tests/test_mcp_portfolio_tools.py`, `tests/test_paper_portfolio_handler.py`

Output contract (matching existing `get_cash_balance_impl`):
```python
{
  "accounts": [
    {"account": "paper:default", "account_name": "default", "broker": "paper",
     "currency": "KRW", "balance": 10_000_000.0, "orderable": 10_000_000.0,
     "formatted": "10,000,000 KRW"},
    {"account": "paper:default", "account_name": "default", "broker": "paper",
     "currency": "USD", "balance": 500.0, "orderable": 500.0,
     "exchange_rate": None, "formatted": "$500.00 USD"},
  ],
  "summary": {"total_krw": 10_000_000.0, "total_usd": 500.0},
  "errors": [],
}
```

Paper has no pending-order concept, so `orderable == balance` for paper rows. USD rows include `"exchange_rate": None` to match the existing KIS-overseas shape.

- [ ] **Step 1: Write the failing handler-level test**

```python
# Append to tests/test_paper_portfolio_handler.py
from app.mcp_server.tooling.paper_portfolio_handler import (
    collect_paper_cash_balances,
)


@pytest.mark.asyncio
async def test_collect_paper_cash_balances_all_accounts(monkeypatch):
    svc = _FakePaperService(
        accounts=[_FakePaperAccount(1, "default"), _FakePaperAccount(2, "day")],
        positions_by_account={},
        cash_by_account={
            1: {"krw": Decimal("10000000"), "usd": Decimal("500")},
            2: {"krw": Decimal("5000000"), "usd": Decimal("0")},
        },
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.paper_portfolio_handler._build_service",
        lambda db: svc,
    )

    rows, errors = await collect_paper_cash_balances(
        selector=PaperAccountSelector(account_name=None),
    )

    assert errors == []
    # 2 accounts × 2 currencies, but USD=0 rows are still emitted for symmetry
    assert len(rows) == 4
    d_krw = next(r for r in rows if r["account"] == "paper:default"
                 and r["currency"] == "KRW")
    assert d_krw["balance"] == 10_000_000.0
    assert d_krw["orderable"] == 10_000_000.0
    assert d_krw["broker"] == "paper"
    assert d_krw["formatted"] == "10,000,000 KRW"
    d_usd = next(r for r in rows if r["account"] == "paper:default"
                 and r["currency"] == "USD")
    assert d_usd["balance"] == 500.0
    assert d_usd["exchange_rate"] is None
    assert d_usd["formatted"] == "$500.00 USD"


@pytest.mark.asyncio
async def test_collect_paper_cash_balances_missing_named_account(monkeypatch):
    svc = _FakePaperService(accounts=[], positions_by_account={})
    monkeypatch.setattr(
        "app.mcp_server.tooling.paper_portfolio_handler._build_service",
        lambda db: svc,
    )

    rows, errors = await collect_paper_cash_balances(
        selector=PaperAccountSelector(account_name="ghost"),
    )
    assert rows == []
    assert errors and "ghost" in errors[0]["error"]
```

Run: `uv run pytest tests/test_paper_portfolio_handler.py -v -k "cash"`
Expected: FAIL — `collect_paper_cash_balances` does not exist yet.

- [ ] **Step 2: Implement `collect_paper_cash_balances`**

Append to `app/mcp_server/tooling/paper_portfolio_handler.py`:

```python
async def collect_paper_cash_balances(
    *,
    selector: PaperAccountSelector,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect paper cash balances in the canonical cash-row shape.

    Emits one row per (account, currency) pair for KRW and USD, even when the
    balance is zero. Paper has no pending-orders concept, so ``orderable``
    always mirrors ``balance``.
    """
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    async with AsyncSessionLocal() as db:
        service = _build_service(db)
        target_accounts, lookup_errors = await _resolve_target_accounts(
            service, selector
        )
        errors.extend(lookup_errors)

        for account in target_accounts:
            try:
                cash = await service.get_cash_balance(account.id)
            except Exception as exc:
                errors.append(
                    {
                        "source": "paper",
                        "account": f"paper:{account.name}",
                        "error": str(exc),
                    }
                )
                continue

            krw = float(cash.get("krw", 0) or 0)
            usd = float(cash.get("usd", 0) or 0)

            rows.append(
                {
                    "account": f"paper:{account.name}",
                    "account_name": account.name,
                    "broker": "paper",
                    "currency": "KRW",
                    "balance": krw,
                    "orderable": krw,
                    "formatted": f"{int(krw):,} KRW",
                }
            )
            rows.append(
                {
                    "account": f"paper:{account.name}",
                    "account_name": account.name,
                    "broker": "paper",
                    "currency": "USD",
                    "balance": usd,
                    "orderable": usd,
                    "exchange_rate": None,
                    "formatted": f"${usd:.2f} USD",
                }
            )

    return rows, errors
```

Extend `__all__` with `"collect_paper_cash_balances"`.

- [ ] **Step 3: Run handler tests**

Run: `uv run pytest tests/test_paper_portfolio_handler.py -v`
Expected: PASS — all handler tests including the 2 new cash tests.

- [ ] **Step 4: Write the failing MCP-tool-level test**

```python
# Append to tests/test_mcp_portfolio_tools.py
@pytest.mark.asyncio
async def test_get_cash_balance_paper_all(monkeypatch):
    tools = build_tools()
    svc = _StubPaperService(
        accounts=[_StubAcc(1, "default")],
        positions={},
        cash={1: {"krw": Decimal("10000000"), "usd": Decimal("500")}},
    )
    monkeypatch.setattr(paper_portfolio_handler, "_build_service", lambda db: svc)

    result = await tools["get_cash_balance"](account="paper")

    assert {r["currency"] for r in result["accounts"]} == {"KRW", "USD"}
    assert result["summary"]["total_krw"] == 10_000_000.0
    assert result["summary"]["total_usd"] == 500.0
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_get_cash_balance_paper_named(monkeypatch):
    tools = build_tools()
    svc = _StubPaperService(
        accounts=[_StubAcc(1, "default"), _StubAcc(2, "day")],
        positions={},
        cash={
            1: {"krw": Decimal("10000000"), "usd": Decimal("0")},
            2: {"krw": Decimal("1000000"), "usd": Decimal("0")},
        },
    )
    monkeypatch.setattr(paper_portfolio_handler, "_build_service", lambda db: svc)

    result = await tools["get_cash_balance"](account="paper:day")

    assert all(r["account"] == "paper:day" for r in result["accounts"])
    assert result["summary"]["total_krw"] == 1_000_000.0
```

Run: `uv run pytest tests/test_mcp_portfolio_tools.py -v -k "cash_balance_paper"`
Expected: FAIL — currently `get_cash_balance_impl` does not know about paper tokens.

- [ ] **Step 5: Add paper short-circuit to `get_cash_balance_impl`**

At the top of `get_cash_balance_impl` in `app/mcp_server/tooling/portfolio_cash.py:95`, before the `accounts: list[dict[str, Any]] = []` line, insert:

```python
    from app.mcp_server.tooling.paper_portfolio_handler import (
        collect_paper_cash_balances,
        is_paper_account_token,
        parse_paper_account_token,
    )

    if is_paper_account_token(account):
        selector = parse_paper_account_token(account)
        rows, errors = await collect_paper_cash_balances(selector=selector)
        total_krw = sum(
            float(r.get("balance", 0) or 0) for r in rows if r.get("currency") == "KRW"
        )
        total_usd = sum(
            float(r.get("balance", 0) or 0) for r in rows if r.get("currency") == "USD"
        )
        return {
            "accounts": rows,
            "summary": {"total_krw": total_krw, "total_usd": total_usd},
            "errors": errors,
        }
```

Then update the tool description in `portfolio_holdings.py:1086-1093`:

```python
    @mcp.tool(
        name="get_cash_balance",
        description=(
            "Query available cash balances from all accounts. "
            "Supports Upbit (KRW), KIS domestic (KRW), KIS overseas (USD), "
            "and paper trading accounts (account='paper' or 'paper:<name>'). "
            "Returns detailed balance information including orderable amounts."
        ),
    )
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_mcp_portfolio_tools.py tests/test_paper_portfolio_handler.py -v`
Expected: PASS — including new paper cash tests and all pre-existing tests.

- [ ] **Step 7: Commit**

```bash
git add app/mcp_server/tooling/paper_portfolio_handler.py \
        app/mcp_server/tooling/portfolio_cash.py \
        app/mcp_server/tooling/portfolio_holdings.py \
        tests/test_paper_portfolio_handler.py \
        tests/test_mcp_portfolio_tools.py
git commit -m "feat(mcp): support paper accounts in get_cash_balance"
```

---

### Task 7: `get_available_capital` — paper KRW+USD aggregation

**Files:**
- Modify: `app/mcp_server/tooling/portfolio_cash.py:248-325` (`get_available_capital_impl`).
- Modify: `app/mcp_server/tooling/portfolio_holdings.py:1097-1111` (`get_available_capital` description).
- Modify: `tests/test_mcp_portfolio_tools.py`.

Because `get_available_capital_impl` already delegates cash collection to `get_cash_balance_impl`, paper accounts now automatically flow through after Task 6. The only extra work: ensure the USD→KRW conversion applies to paper USD rows too, and that `include_manual` does not inject the live-account manual cash when the user explicitly asked for paper. Paper queries should return `manual_cash = None`.

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_mcp_portfolio_tools.py
@pytest.mark.asyncio
async def test_get_available_capital_paper(monkeypatch):
    tools = build_tools()
    svc = _StubPaperService(
        accounts=[_StubAcc(1, "default")],
        positions={},
        cash={1: {"krw": Decimal("10000000"), "usd": Decimal("500")}},
    )
    monkeypatch.setattr(paper_portfolio_handler, "_build_service", lambda db: svc)
    # Exchange-rate fetch — stub to a deterministic value.
    monkeypatch.setattr(
        "app.mcp_server.tooling.portfolio_cash.get_usd_krw_rate",
        AsyncMock(return_value=1400.0),
    )
    # Manual cash must not be added for paper queries.
    monkeypatch.setattr(
        "app.mcp_server.tooling.portfolio_cash.get_manual_cash_setting",
        AsyncMock(side_effect=AssertionError("manual cash must not be queried")),
    )

    result = await tools["get_available_capital"](account="paper")

    assert result["manual_cash"] is None
    # 10,000,000 KRW + 500 USD * 1400 = 10,700,000
    assert result["summary"]["total_orderable_krw"] == 10_700_000.0
    assert result["summary"]["exchange_rate_usd_krw"] == 1400.0
    # paper USD row must have krw_equivalent injected
    usd_row = next(r for r in result["accounts"] if r["currency"] == "USD")
    assert usd_row["krw_equivalent"] == 700_000.0
```

Run: `uv run pytest tests/test_mcp_portfolio_tools.py -v -k "available_capital_paper"`
Expected: FAIL — `get_manual_cash_setting` is still called unconditionally.

- [ ] **Step 2: Short-circuit manual-cash for paper**

In `get_available_capital_impl` (`portfolio_cash.py:248`), after the `cash_result = await get_cash_balance_impl(account=account)` line, detect paper mode and skip manual cash:

Replace:
```python
    manual_cash_result: dict[str, Any] | None = None
    if include_manual:
```
With:
```python
    from app.mcp_server.tooling.paper_portfolio_handler import (
        is_paper_account_token,
    )
    manual_cash_result: dict[str, Any] | None = None
    if include_manual and not is_paper_account_token(account):
```

The USD→KRW conversion loop above already runs unconditionally for any row whose currency is USD, so paper USD rows already get `krw_equivalent` injected — no further change needed for conversion.

- [ ] **Step 3: Update tool description**

Replace the `get_available_capital` registration description at `portfolio_holdings.py:1097-1104`:

```python
    @mcp.tool(
        name="get_available_capital",
        description=(
            "Query orderable capital across KIS, Upbit, manual cash, and "
            "paper trading accounts (account='paper' or 'paper:<name>'). "
            "Converts USD orderable cash to KRW and can optionally exclude "
            "manual cash. Manual cash is stored via set_user_setting/"
            "get_user_setting with key='manual_cash'; it is not added for "
            "paper account queries."
        ),
    )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_mcp_portfolio_tools.py tests/test_paper_portfolio_handler.py -v`
Expected: PASS — all tests, including the new `get_available_capital_paper` case.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/portfolio_cash.py \
        app/mcp_server/tooling/portfolio_holdings.py \
        tests/test_mcp_portfolio_tools.py
git commit -m "feat(mcp): support paper accounts in get_available_capital"
```

---

### Task 8: Regression sweep — full portfolio tool suite

**Files:** None (verification only).

- [ ] **Step 1: Run the full tool + handler suites**

Run:
```bash
uv run pytest tests/test_mcp_portfolio_tools.py \
              tests/test_paper_portfolio_handler.py \
              tests/test_paper_trading_service.py \
              -v
```
Expected: PASS — no regressions in live-broker tests.

- [ ] **Step 2: Lint + typecheck**

Run:
```bash
make lint
make typecheck
```
Expected: no new errors introduced by the changes.

- [ ] **Step 3: If lint / typecheck reports issues, fix in place and re-run**

Common fixes:
- Missing imports in `portfolio_holdings.py` / `portfolio_cash.py`.
- `Literal["real", "paper"]` annotation for `account_type` if typecheck wants it tightened — import from `typing`.

- [ ] **Step 4: Final commit if any follow-up fixes were needed**

```bash
git add -u
git commit -m "chore(mcp): lint/typecheck fixes for paper portfolio integration"
```

---

## Self-Review Checklist (performed, findings applied)

- **Spec coverage:**
  - `get_holdings account=paper[:...]` → Task 4 ✓
  - `get_position` `account_type` + `paper_account` params → Task 5 ✓
  - `get_cash_balance account=paper` → Task 6 ✓
  - `get_available_capital account=paper` KRW + USD aggregation → Task 7 ✓
  - New file `paper_portfolio_handler.py` → Tasks 1–3, 6 ✓
  - Real-portfolio logic untouched → every task keeps real branches unmodified ✓
  - `"account"` field formatted as `paper:<name>` → Task 3 (`_paper_position_to_canonical`), Task 6 (cash row) ✓
  - `include_current_price=True` reuses existing real-time quote plumbing → Task 4 calls `_fetch_price_map_for_positions` + `_recalculate_profit_fields` ✓
- **Placeholders:** none found on rescan.
- **Type consistency:**
  - `PaperAccountSelector`, `parse_paper_account_token`, `is_paper_account_token`, `collect_paper_positions`, `collect_paper_cash_balances`, `resolve_paper_position_name`, `_build_service`, `_resolve_target_accounts`, `_paper_position_to_canonical` — referenced consistently across tasks.
  - Canonical position shape (`avg_buy_price`, `evaluation_amount`, `profit_loss`, `profit_rate`) is reused across Task 3 translator and Task 4 price-fill path.
  - Cash row shape matches existing keys (`account`, `account_name`, `broker`, `currency`, `balance`, `orderable`, `formatted`, optional `exchange_rate`).

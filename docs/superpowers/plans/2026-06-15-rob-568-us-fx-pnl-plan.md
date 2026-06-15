# ROB-568 US FX PnL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture approximate USD/KRW FX rates at US buy/sell reconcile time, attribute FIFO FX PnL separately from native security PnL, and expose manual exact override paths for legacy or operator-corrected trades.

**Architecture:** Add nullable FX columns to journals, Toss live ledger, generic US live ledger, and retrospectives. Keep the accounting math in one focused helper module, keep FIFO attribution in `order_journal._close_journals_on_sell`, and have Toss/KIS-US reconcile only capture spot FX and persist the returned journal attribution. Toss order-detail does not provide fill-time FX; all automatic values are labelled `fx_rate_source="reconcile_spot"` and `fx_pnl_accuracy="approximate"`.

**Tech Stack:** Python 3.13, SQLAlchemy ORM, Alembic, FastMCP tool handlers, pytest, `uv`.

---

## Grounding

- Linear: ROB-568.
- Confirmed decision: Toss `GET /orders/{id}` execution has no FX fields in OpenAPI v1.1.1. Automatic FX rate capture uses `app.services.exchange_rate_service.get_usd_krw_rate_details()` at reconcile time.
- Current Alembic head: `20260615_rob569_toss_review`.
- Risk labels to apply before merge: `high_risk_change`, `needs_stronger_model_review`, `hold_for_final_review`.
- Scope: US only. KR domestic `kis_live_reconcile_orders` remains unchanged except shared helper imports if needed.

## Data Contract

Use these nullable columns consistently:

- `buy_fx_rate`: USD/KRW used for the FIFO buy lot.
- `sell_fx_rate`: USD/KRW used for the sell close.
- `fx_pnl_krw`: pure FX effect in KRW.
- `security_pnl_usd`: native USD security PnL, before FX effect.
- `security_pnl_krw`: `security_pnl_usd * sell_fx_rate`.
- `total_pnl_krw`: `security_pnl_krw + fx_pnl_krw`.
- `fx_rate_source`: `reconcile_spot`, `manual`, `unavailable`.
- `fx_pnl_accuracy`: `approximate`, `exact`, `unavailable`.

For one FIFO-closed journal lot:

```python
buy_notional_usd = entry_price * closed_qty
sell_notional_usd = sell_price * closed_qty
security_pnl_usd = sell_notional_usd - buy_notional_usd
security_pnl_krw = security_pnl_usd * sell_fx_rate
fx_pnl_krw = buy_notional_usd * (sell_fx_rate - buy_fx_rate)
total_pnl_krw = security_pnl_krw + fx_pnl_krw
```

This matches the Toss UI split: stock/security movement is measured in USD, then FX effect is applied to the original USD cost basis. Algebraically:

```python
total_pnl_krw == (sell_notional_usd * sell_fx_rate) - (buy_notional_usd * buy_fx_rate)
```

Legacy holdings with no captured buy FX keep `buy_fx_rate=None`, `fx_pnl_krw=None`, `security_pnl_krw=None`, `total_pnl_krw=None`, and `fx_pnl_accuracy="unavailable"` until an operator supplies exact values through `update_trade_journal`.

## File Structure

- Create: `app/mcp_server/tooling/fx_pnl.py` — pure FX formula helpers plus reconcile-spot capture wrapper.
- Modify: `app/models/trade_journal.py` — add nullable FX columns to `TradeJournal`.
- Modify: `app/models/review.py` — add nullable FX columns to `LiveOrderLedger`, `TossLiveOrderLedger`, and `TradeRetrospective`; add `toss_live` to retrospective account-mode constraint.
- Create: `alembic/versions/20260615_rob568_us_fx_pnl.py` — add/drop FX columns and replace retrospective account-mode check.
- Modify: `app/mcp_server/tooling/order_journal.py` — accept buy FX at journal creation; compute FIFO FX PnL at journal close; return aggregate FX fields.
- Modify: `app/mcp_server/tooling/toss_live_ledger.py` — capture US buy/sell reconcile spot FX, pass it to journal helpers, persist ledger FX outcome.
- Modify: `app/services/toss_live_order_ledger_service.py` — persist Toss ledger FX fields.
- Modify: `app/mcp_server/tooling/live_order_ledger.py` — capture US KIS generic live reconcile spot FX and persist ledger FX outcome.
- Modify: `app/mcp_server/tooling/trade_journal_tools.py` — serialize FX fields; allow manual exact override via `save_trade_journal` and `update_trade_journal`.
- Modify: `app/mcp_server/tooling/trade_journal_registration.py` — document manual FX override parameters.
- Modify: `app/services/trade_journal/trade_retrospective_service.py` — accept, derive, serialize, list, and aggregate FX fields; allow `account_mode="toss_live"`.
- Modify: `app/mcp_server/tooling/trade_retrospective_tools.py` — expose FX parameters for `save_trade_retrospective`.
- Modify: docs in `app/mcp_server/README.md` and `docs/runbooks/toss-live-order-reconcile.md`.
- Test: extend existing model/reconcile/retrospective tests and add `tests/mcp_server/tooling/test_fx_pnl.py`.

---

### Task 1: Pure FX PnL Helper

**Files:**
- Create: `app/mcp_server/tooling/fx_pnl.py`
- Create: `tests/mcp_server/tooling/test_fx_pnl.py`

- [ ] **Step 1: Write failing helper tests**

Create `tests/mcp_server/tooling/test_fx_pnl.py`:

```python
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit


def test_compute_us_fx_pnl_matches_total_identity():
    from app.mcp_server.tooling.fx_pnl import compute_us_equity_fx_pnl

    result = compute_us_equity_fx_pnl(
        buy_price=Decimal("100"),
        sell_price=Decimal("130"),
        quantity=Decimal("2"),
        buy_fx_rate=Decimal("1389.33"),
        sell_fx_rate=Decimal("1503.19"),
    )

    assert result["buy_notional_usd"] == Decimal("200")
    assert result["sell_notional_usd"] == Decimal("260")
    assert result["security_pnl_usd"] == Decimal("60")
    assert result["security_pnl_krw"] == Decimal("90191.4000")
    assert result["fx_pnl_krw"] == Decimal("22772.0000")
    assert result["total_pnl_krw"] == Decimal("112963.4000")


def test_compute_us_fx_pnl_returns_none_when_buy_fx_missing():
    from app.mcp_server.tooling.fx_pnl import compute_us_equity_fx_pnl

    assert (
        compute_us_equity_fx_pnl(
            buy_price=Decimal("100"),
            sell_price=Decimal("130"),
            quantity=Decimal("2"),
            buy_fx_rate=None,
            sell_fx_rate=Decimal("1503.19"),
        )
        is None
    )


@pytest.mark.asyncio
async def test_capture_reconcile_spot_labels_approximate(monkeypatch):
    from app.mcp_server.tooling import fx_pnl

    quote = fx_pnl.UsdKrwExchangeRateQuote(
        rate=1503.19,
        mid_rate=1503.19,
        source="toss",
    )
    monkeypatch.setattr(
        fx_pnl,
        "get_usd_krw_rate_details",
        AsyncMock(return_value=quote),
    )

    captured = await fx_pnl.capture_reconcile_spot_fx()

    assert captured.rate == Decimal("1503.19")
    assert captured.fx_rate_source == "reconcile_spot"
    assert captured.fx_pnl_accuracy == "approximate"


@pytest.mark.asyncio
async def test_capture_reconcile_spot_fails_open(monkeypatch):
    from app.mcp_server.tooling import fx_pnl

    monkeypatch.setattr(
        fx_pnl,
        "get_usd_krw_rate_details",
        AsyncMock(side_effect=RuntimeError("fx down")),
    )

    captured = await fx_pnl.capture_reconcile_spot_fx()

    assert captured.rate is None
    assert captured.fx_rate_source == "unavailable"
    assert captured.fx_pnl_accuracy == "unavailable"
```

- [ ] **Step 2: Run helper tests to verify failure**

Run:

```bash
uv run pytest tests/mcp_server/tooling/test_fx_pnl.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.mcp_server.tooling.fx_pnl'`.

- [ ] **Step 3: Implement helper module**

Create `app/mcp_server/tooling/fx_pnl.py`:

```python
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from app.services.exchange_rate_service import (
    UsdKrwExchangeRateQuote,
    get_usd_krw_rate_details,
)

logger = logging.getLogger(__name__)

FX_RATE_SOURCE_RECONCILE_SPOT = "reconcile_spot"
FX_RATE_SOURCE_MANUAL = "manual"
FX_RATE_SOURCE_UNAVAILABLE = "unavailable"
FX_PNL_ACCURACY_APPROXIMATE = "approximate"
FX_PNL_ACCURACY_EXACT = "exact"
FX_PNL_ACCURACY_UNAVAILABLE = "unavailable"

_MONEY_4 = Decimal("0.0001")


@dataclass(frozen=True)
class FxRateCapture:
    rate: Decimal | None
    fx_rate_source: str
    fx_pnl_accuracy: str


def _q4(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_4, rounding=ROUND_HALF_UP)


def compute_us_equity_fx_pnl(
    *,
    buy_price: Decimal,
    sell_price: Decimal,
    quantity: Decimal,
    buy_fx_rate: Decimal | None,
    sell_fx_rate: Decimal | None,
) -> dict[str, Decimal] | None:
    if buy_fx_rate is None or sell_fx_rate is None:
        return None
    if quantity <= 0:
        return None

    buy_notional_usd = buy_price * quantity
    sell_notional_usd = sell_price * quantity
    security_pnl_usd = sell_notional_usd - buy_notional_usd
    security_pnl_krw = security_pnl_usd * sell_fx_rate
    fx_pnl_krw = buy_notional_usd * (sell_fx_rate - buy_fx_rate)
    total_pnl_krw = security_pnl_krw + fx_pnl_krw
    identity_total = (sell_notional_usd * sell_fx_rate) - (
        buy_notional_usd * buy_fx_rate
    )

    return {
        "buy_notional_usd": _q4(buy_notional_usd),
        "sell_notional_usd": _q4(sell_notional_usd),
        "security_pnl_usd": _q4(security_pnl_usd),
        "security_pnl_krw": _q4(security_pnl_krw),
        "fx_pnl_krw": _q4(fx_pnl_krw),
        "total_pnl_krw": _q4(total_pnl_krw),
        "identity_total_pnl_krw": _q4(identity_total),
    }


async def capture_reconcile_spot_fx() -> FxRateCapture:
    try:
        quote = await get_usd_krw_rate_details()
    except Exception as exc:
        logger.warning("USD/KRW reconcile-spot capture failed: %s", exc)
        return FxRateCapture(
            rate=None,
            fx_rate_source=FX_RATE_SOURCE_UNAVAILABLE,
            fx_pnl_accuracy=FX_PNL_ACCURACY_UNAVAILABLE,
        )
    return FxRateCapture(
        rate=Decimal(str(quote.default_rate)),
        fx_rate_source=FX_RATE_SOURCE_RECONCILE_SPOT,
        fx_pnl_accuracy=FX_PNL_ACCURACY_APPROXIMATE,
    )


__all__ = [
    "FX_PNL_ACCURACY_APPROXIMATE",
    "FX_PNL_ACCURACY_EXACT",
    "FX_PNL_ACCURACY_UNAVAILABLE",
    "FX_RATE_SOURCE_MANUAL",
    "FX_RATE_SOURCE_RECONCILE_SPOT",
    "FX_RATE_SOURCE_UNAVAILABLE",
    "FxRateCapture",
    "UsdKrwExchangeRateQuote",
    "capture_reconcile_spot_fx",
    "compute_us_equity_fx_pnl",
]
```

- [ ] **Step 4: Run helper tests**

Run:

```bash
uv run pytest tests/mcp_server/tooling/test_fx_pnl.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit helper**

```bash
git add app/mcp_server/tooling/fx_pnl.py tests/mcp_server/tooling/test_fx_pnl.py
git commit -m "feat(ROB-568): add US FX PnL helper"
```

---

### Task 2: Schema And ORM Fields

**Files:**
- Modify: `app/models/trade_journal.py`
- Modify: `app/models/review.py`
- Create: `alembic/versions/20260615_rob568_us_fx_pnl.py`
- Modify: `tests/test_trade_journal_model.py`
- Modify: `tests/test_rob538_toss_live_ledger_schema.py`
- Modify: `tests/mcp_server/tooling/test_live_order_ledger.py`
- Modify: `tests/test_trade_retrospective_model.py`

- [ ] **Step 1: Write failing model shape tests**

Append to `tests/test_trade_journal_model.py`:

```python
def test_us_fx_columns_present_on_trade_journal():
    cols = set(TradeJournal.__table__.columns.keys())
    for col in (
        "buy_fx_rate",
        "sell_fx_rate",
        "fx_pnl_krw",
        "security_pnl_usd",
        "security_pnl_krw",
        "total_pnl_krw",
        "fx_rate_source",
        "fx_pnl_accuracy",
    ):
        assert col in cols, f"missing column {col}"
```

Extend `tests/test_rob538_toss_live_ledger_schema.py::test_toss_live_order_ledger_model_shape` expected column tuple with:

```python
"buy_fx_rate",
"sell_fx_rate",
"fx_pnl_krw",
"security_pnl_usd",
"security_pnl_krw",
"total_pnl_krw",
"fx_rate_source",
"fx_pnl_accuracy",
```

Extend `tests/mcp_server/tooling/test_live_order_ledger.py::test_live_order_ledger_model_shape` expected columns with the same eight names.

Append to `tests/test_trade_retrospective_model.py`:

```python
def test_trade_retrospective_us_fx_columns_present():
    cols = set(TradeRetrospective.__table__.columns.keys())
    for col in (
        "buy_fx_rate",
        "sell_fx_rate",
        "fx_pnl_krw",
        "security_pnl_usd",
        "security_pnl_krw",
        "total_pnl_krw",
        "fx_rate_source",
        "fx_pnl_accuracy",
    ):
        assert col in cols, f"missing column {col}"
```

- [ ] **Step 2: Run model tests to verify failure**

Run:

```bash
uv run pytest \
  tests/test_trade_journal_model.py::test_us_fx_columns_present_on_trade_journal \
  tests/test_rob538_toss_live_ledger_schema.py::test_toss_live_order_ledger_model_shape \
  tests/mcp_server/tooling/test_live_order_ledger.py::test_live_order_ledger_model_shape \
  tests/test_trade_retrospective_model.py::test_trade_retrospective_us_fx_columns_present \
  -q
```

Expected: FAIL on missing FX columns.

- [ ] **Step 3: Add ORM columns**

In `app/models/trade_journal.py`, add after `pnl_pct`:

```python
    buy_fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    sell_fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    fx_pnl_krw: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    security_pnl_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    security_pnl_krw: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    total_pnl_krw: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    fx_rate_source: Mapped[str | None] = mapped_column(Text)
    fx_pnl_accuracy: Mapped[str | None] = mapped_column(Text)
```

In `app/models/review.py`, add the same eight mapped columns to:

- `LiveOrderLedger`, immediately after `reconciled_at`.
- `TossLiveOrderLedger`, immediately after `reconciled_at`.
- `TradeRetrospective`, immediately after `pnl_pct`.

Also update the `TradeRetrospective` account-mode check to include `toss_live`:

```python
"account_mode IN ('kis_mock','kiwoom_mock','kis_live','toss_live','alpaca_paper','upbit_live')"
```

- [ ] **Step 4: Create migration**

Create `alembic/versions/20260615_rob568_us_fx_pnl.py`:

```python
"""ROB-568 add US FX PnL fields.

Revision ID: 20260615_rob568_us_fx_pnl
Revises: 20260615_rob569_toss_review
Create Date: 2026-06-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260615_rob568_us_fx_pnl"
down_revision: str | Sequence[str] | None = "20260615_rob569_toss_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FX_COLUMNS = (
    sa.Column("buy_fx_rate", sa.Numeric(18, 4), nullable=True),
    sa.Column("sell_fx_rate", sa.Numeric(18, 4), nullable=True),
    sa.Column("fx_pnl_krw", sa.Numeric(20, 4), nullable=True),
    sa.Column("security_pnl_usd", sa.Numeric(20, 4), nullable=True),
    sa.Column("security_pnl_krw", sa.Numeric(20, 4), nullable=True),
    sa.Column("total_pnl_krw", sa.Numeric(20, 4), nullable=True),
    sa.Column("fx_rate_source", sa.Text(), nullable=True),
    sa.Column("fx_pnl_accuracy", sa.Text(), nullable=True),
)

TABLES = (
    "trade_journals",
    "live_order_ledger",
    "toss_live_order_ledger",
    "trade_retrospectives",
)


def _add_fx_columns(table_name: str) -> None:
    for column in FX_COLUMNS:
        op.add_column(table_name, column.copy(), schema="review")


def _drop_fx_columns(table_name: str) -> None:
    for name in reversed([column.name for column in FX_COLUMNS]):
        op.drop_column(table_name, name, schema="review")


def upgrade() -> None:
    for table_name in TABLES:
        _add_fx_columns(table_name)

    op.drop_constraint(
        "ck_trade_retrospectives_account_mode",
        "trade_retrospectives",
        schema="review",
        type_="check",
    )
    op.create_check_constraint(
        "ck_trade_retrospectives_account_mode",
        "trade_retrospectives",
        "account_mode IN ('kis_mock','kiwoom_mock','kis_live','toss_live','alpaca_paper','upbit_live')",
        schema="review",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_trade_retrospectives_account_mode",
        "trade_retrospectives",
        schema="review",
        type_="check",
    )
    op.create_check_constraint(
        "ck_trade_retrospectives_account_mode",
        "trade_retrospectives",
        "account_mode IN ('kis_mock','kiwoom_mock','kis_live','alpaca_paper','upbit_live')",
        schema="review",
    )

    for table_name in reversed(TABLES):
        _drop_fx_columns(table_name)
```

- [ ] **Step 5: Run schema tests and Alembic head check**

Run:

```bash
uv run pytest \
  tests/test_trade_journal_model.py::test_us_fx_columns_present_on_trade_journal \
  tests/test_rob538_toss_live_ledger_schema.py::test_toss_live_order_ledger_model_shape \
  tests/mcp_server/tooling/test_live_order_ledger.py::test_live_order_ledger_model_shape \
  tests/test_trade_retrospective_model.py::test_trade_retrospective_us_fx_columns_present \
  -q
uv run alembic heads
```

Expected: tests PASS, Alembic shows one head: `20260615_rob568_us_fx_pnl (head)`.

- [ ] **Step 6: Commit schema**

```bash
git add app/models/trade_journal.py app/models/review.py alembic/versions/20260615_rob568_us_fx_pnl.py tests/test_trade_journal_model.py tests/test_rob538_toss_live_ledger_schema.py tests/mcp_server/tooling/test_live_order_ledger.py tests/test_trade_retrospective_model.py
git commit -m "feat(ROB-568): add US FX PnL schema"
```

---

### Task 3: Journal FIFO FX Attribution

**Files:**
- Modify: `app/mcp_server/tooling/order_journal.py`
- Modify: `tests/test_mcp_trade_journal.py`

- [ ] **Step 1: Add failing order journal tests**

Append to `tests/test_mcp_trade_journal.py`:

```python
@pytest.mark.asyncio
async def test_create_buy_journal_persists_reconcile_spot_fx():
    from app.mcp_server.tooling.order_journal import _create_trade_journal_for_buy
    from app.models.trade_journal import TradeJournal

    result = await _create_trade_journal_for_buy(
        symbol="AAPL",
        market_type="equity_us",
        preview={"price": 100.0, "quantity": 2.0, "estimated_value": 200.0},
        thesis="t",
        strategy="s",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
        account_type="live",
        account="toss",
        buy_fx_rate=1500.25,
        fx_rate_source="reconcile_spot",
        fx_pnl_accuracy="approximate",
    )

    async with _order_session_factory()() as db:
        journal = await db.get(TradeJournal, result["journal_id"])

    assert journal.buy_fx_rate == Decimal("1500.2500")
    assert journal.fx_rate_source == "reconcile_spot"
    assert journal.fx_pnl_accuracy == "approximate"


@pytest.mark.asyncio
async def test_close_journals_computes_fifo_fx_pnl_for_us_equity(db_session):
    from app.mcp_server.tooling.order_journal import _close_journals_on_sell
    from app.models.trade_journal import TradeJournal

    j = TradeJournal(
        symbol="AAPL",
        instrument_type="equity_us",
        side="buy",
        entry_price=Decimal("100"),
        quantity=Decimal("2"),
        amount=Decimal("200"),
        thesis="t",
        account_type="live",
        account="toss",
        status="active",
        buy_fx_rate=Decimal("1389.33"),
        fx_rate_source="reconcile_spot",
        fx_pnl_accuracy="approximate",
    )
    db_session.add(j)
    await db_session.commit()
    await db_session.refresh(j)

    close_result = await _close_journals_on_sell(
        symbol="AAPL",
        sell_quantity=2.0,
        sell_price=130.0,
        exit_reason="sold",
        account_type="live",
        account="toss",
        sell_fx_rate=1503.19,
        fx_rate_source="reconcile_spot",
        fx_pnl_accuracy="approximate",
    )

    refreshed = await db_session.get(TradeJournal, j.id)
    assert refreshed.fx_pnl_krw == Decimal("22772.0000")
    assert refreshed.security_pnl_usd == Decimal("60.0000")
    assert refreshed.security_pnl_krw == Decimal("90191.4000")
    assert refreshed.total_pnl_krw == Decimal("112963.4000")
    assert refreshed.sell_fx_rate == Decimal("1503.1900")
    assert close_result["fx_pnl_krw"] == pytest.approx(22772.0)
    assert close_result["total_pnl_krw"] == pytest.approx(112963.4)
    assert close_result["fx_pnl_accuracy"] == "approximate"


@pytest.mark.asyncio
async def test_close_journals_marks_fx_unavailable_when_buy_rate_missing(db_session):
    from app.mcp_server.tooling.order_journal import _close_journals_on_sell
    from app.models.trade_journal import TradeJournal

    j = TradeJournal(
        symbol="MSFT",
        instrument_type="equity_us",
        side="buy",
        entry_price=Decimal("100"),
        quantity=Decimal("1"),
        amount=Decimal("100"),
        thesis="legacy",
        account_type="live",
        account="toss",
        status="active",
    )
    db_session.add(j)
    await db_session.commit()
    await db_session.refresh(j)

    close_result = await _close_journals_on_sell(
        symbol="MSFT",
        sell_quantity=1.0,
        sell_price=130.0,
        exit_reason="sold",
        account_type="live",
        account="toss",
        sell_fx_rate=1503.19,
        fx_rate_source="reconcile_spot",
        fx_pnl_accuracy="approximate",
    )

    refreshed = await db_session.get(TradeJournal, j.id)
    assert refreshed.sell_fx_rate == Decimal("1503.1900")
    assert refreshed.fx_pnl_krw is None
    assert refreshed.fx_pnl_accuracy == "unavailable"
    assert close_result["fx_pnl_accuracy"] == "unavailable"
    assert close_result["fx_unavailable_journal_ids"] == [j.id]
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest \
  tests/test_mcp_trade_journal.py::test_create_buy_journal_persists_reconcile_spot_fx \
  tests/test_mcp_trade_journal.py::test_close_journals_computes_fifo_fx_pnl_for_us_equity \
  tests/test_mcp_trade_journal.py::test_close_journals_marks_fx_unavailable_when_buy_rate_missing \
  -q
```

Expected: FAIL because `_create_trade_journal_for_buy` and `_close_journals_on_sell` do not accept FX parameters.

- [ ] **Step 3: Extend `_create_trade_journal_for_buy`**

In `app/mcp_server/tooling/order_journal.py`, extend the function signature:

```python
    buy_fx_rate: float | None = None,
    fx_rate_source: str | None = None,
    fx_pnl_accuracy: str | None = None,
```

Add to the `TradeJournal(...)` constructor:

```python
        buy_fx_rate=Decimal(str(buy_fx_rate)) if buy_fx_rate is not None else None,
        fx_rate_source=fx_rate_source,
        fx_pnl_accuracy=fx_pnl_accuracy,
```

- [ ] **Step 4: Extend `_close_journals_on_sell`**

Import helper constants and formula:

```python
from app.mcp_server.tooling.fx_pnl import (
    FX_PNL_ACCURACY_UNAVAILABLE,
    FX_RATE_SOURCE_UNAVAILABLE,
    compute_us_equity_fx_pnl,
)
```

Extend `_close_journals_on_sell` signature:

```python
    sell_fx_rate: float | None = None,
    fx_rate_source: str | None = None,
    fx_pnl_accuracy: str | None = None,
```

Before the FIFO loop, initialize aggregation:

```python
        fx_pnl_sum = Decimal("0")
        security_pnl_usd_sum = Decimal("0")
        security_pnl_krw_sum = Decimal("0")
        total_pnl_krw_sum = Decimal("0")
        fx_buy_notional_sum = Decimal("0")
        fx_buy_weighted_sum = Decimal("0")
        fx_computed_count = 0
        fx_unavailable_journal_ids: list[int] = []
        sell_fx_rate_dec = (
            Decimal(str(sell_fx_rate)) if sell_fx_rate is not None else None
        )
```

Inside the branch that closes a journal, after `journal.pnl_pct` calculation, add:

```python
            if (
                journal.instrument_type == InstrumentType.equity_us
                and journal_qty is not None
                and journal.entry_price is not None
            ):
                journal.sell_fx_rate = sell_fx_rate_dec
                fx_values = compute_us_equity_fx_pnl(
                    buy_price=Decimal(str(journal.entry_price)),
                    sell_price=sell_price_dec,
                    quantity=Decimal(str(journal_qty)),
                    buy_fx_rate=Decimal(str(journal.buy_fx_rate))
                    if journal.buy_fx_rate is not None
                    else None,
                    sell_fx_rate=sell_fx_rate_dec,
                )
                if fx_values is None:
                    journal.fx_rate_source = FX_RATE_SOURCE_UNAVAILABLE
                    journal.fx_pnl_accuracy = FX_PNL_ACCURACY_UNAVAILABLE
                    fx_unavailable_journal_ids.append(journal.id)
                else:
                    journal.security_pnl_usd = fx_values["security_pnl_usd"]
                    journal.security_pnl_krw = fx_values["security_pnl_krw"]
                    journal.fx_pnl_krw = fx_values["fx_pnl_krw"]
                    journal.total_pnl_krw = fx_values["total_pnl_krw"]
                    journal.fx_rate_source = fx_rate_source
                    journal.fx_pnl_accuracy = fx_pnl_accuracy
                    fx_pnl_sum += fx_values["fx_pnl_krw"]
                    security_pnl_usd_sum += fx_values["security_pnl_usd"]
                    security_pnl_krw_sum += fx_values["security_pnl_krw"]
                    total_pnl_krw_sum += fx_values["total_pnl_krw"]
                    fx_buy_notional_sum += fx_values["buy_notional_usd"]
                    fx_buy_weighted_sum += (
                        fx_values["buy_notional_usd"]
                        * Decimal(str(journal.buy_fx_rate))
                    )
                    fx_computed_count += 1
```

Extend the returned dict with:

```python
        "buy_fx_rate": float(fx_buy_weighted_sum / fx_buy_notional_sum)
        if fx_buy_notional_sum > 0
        else None,
        "sell_fx_rate": float(sell_fx_rate_dec) if sell_fx_rate_dec is not None else None,
        "fx_pnl_krw": float(fx_pnl_sum) if fx_computed_count else None,
        "security_pnl_usd": float(security_pnl_usd_sum) if fx_computed_count else None,
        "security_pnl_krw": float(security_pnl_krw_sum) if fx_computed_count else None,
        "total_pnl_krw": float(total_pnl_krw_sum) if fx_computed_count else None,
        "fx_rate_source": fx_rate_source if fx_computed_count else FX_RATE_SOURCE_UNAVAILABLE,
        "fx_pnl_accuracy": fx_pnl_accuracy if fx_computed_count else FX_PNL_ACCURACY_UNAVAILABLE,
        "fx_unavailable_journal_ids": fx_unavailable_journal_ids,
```

- [ ] **Step 5: Run journal tests**

Run:

```bash
uv run pytest \
  tests/test_mcp_trade_journal.py::test_create_buy_journal_persists_reconcile_spot_fx \
  tests/test_mcp_trade_journal.py::test_close_journals_computes_fifo_fx_pnl_for_us_equity \
  tests/test_mcp_trade_journal.py::test_close_journals_marks_fx_unavailable_when_buy_rate_missing \
  -q
```

Expected: PASS.

- [ ] **Step 6: Commit FIFO attribution**

```bash
git add app/mcp_server/tooling/order_journal.py tests/test_mcp_trade_journal.py
git commit -m "feat(ROB-568): attribute US FX PnL on journal close"
```

---

### Task 4: Toss US Reconcile Capture And Persistence

**Files:**
- Modify: `app/mcp_server/tooling/toss_live_ledger.py`
- Modify: `app/services/toss_live_order_ledger_service.py`
- Modify: `tests/mcp_server/tooling/test_toss_live_ledger.py`
- Modify: `tests/services/test_toss_live_order_ledger_service.py`

- [ ] **Step 1: Add failing Toss service persistence test**

Append to `tests/services/test_toss_live_order_ledger_service.py`:

```python
async def test_update_reconcile_outcome_records_us_fx_fields(db_session):
    svc = TossLiveOrderLedgerService(db_session)
    row = await svc.record_send(
        **_place_kwargs(client_order_id="cid-fx", broker_order_id="ord-fx")
    )

    await svc.update_reconcile_outcome(
        ledger_id=row.id,
        status="filled",
        broker_status="FILLED",
        buy_fx_rate=Decimal("1389.33"),
        sell_fx_rate=Decimal("1503.19"),
        fx_pnl_krw=Decimal("22772.00"),
        security_pnl_usd=Decimal("60.00"),
        security_pnl_krw=Decimal("90191.40"),
        total_pnl_krw=Decimal("112963.40"),
        fx_rate_source="reconcile_spot",
        fx_pnl_accuracy="approximate",
    )

    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.buy_fx_rate == Decimal("1389.33")
    assert refreshed.sell_fx_rate == Decimal("1503.19")
    assert refreshed.fx_pnl_krw == Decimal("22772.00")
    assert refreshed.fx_rate_source == "reconcile_spot"
    assert refreshed.fx_pnl_accuracy == "approximate"
```

- [ ] **Step 2: Add failing Toss reconcile tests**

Append to `tests/mcp_server/tooling/test_toss_live_ledger.py`:

```python
async def test_toss_us_buy_reconcile_captures_buy_fx_rate(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.fx_pnl import FxRateCapture
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session, side="buy")
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("100"),
        commission=Decimal("0"),
        tax=Decimal("0"),
        fee_total=Decimal("0"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod,
            "capture_reconcile_spot_fx",
            new=AsyncMock(
                return_value=FxRateCapture(
                    Decimal("1389.33"), "reconcile_spot", "approximate"
                )
            ),
        ),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ) as m_journal,
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["buy_fx_rate"] == pytest.approx(1389.33)
    assert out["fx_rate_source"] == "reconcile_spot"
    assert m_journal.await_args.kwargs["buy_fx_rate"] == 1389.33


async def test_toss_us_sell_reconcile_surfaces_fx_pnl(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.fx_pnl import FxRateCapture
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session, side="sell")
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("130"),
        commission=Decimal("0"),
        tax=Decimal("0"),
        fee_total=Decimal("0"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )
    close_result = {
        "journals_closed": 1,
        "journals_kept": 0,
        "closed_ids": [77],
        "total_pnl_pct": 30.0,
        "realized_pnl_basis": "journal_entry",
        "buy_fx_rate": 1389.33,
        "sell_fx_rate": 1503.19,
        "fx_pnl_krw": 22772.0,
        "security_pnl_usd": 60.0,
        "security_pnl_krw": 90191.4,
        "total_pnl_krw": 112963.4,
        "fx_rate_source": "reconcile_spot",
        "fx_pnl_accuracy": "approximate",
        "fx_unavailable_journal_ids": [],
    }

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod,
            "capture_reconcile_spot_fx",
            new=AsyncMock(
                return_value=FxRateCapture(
                    Decimal("1503.19"), "reconcile_spot", "approximate"
                )
            ),
        ),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod,
            "_close_journals_on_sell",
            new=AsyncMock(return_value=close_result),
        ),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["realized_pnl_basis"] == "journal_entry"
    assert out["fx_pnl_krw"] == pytest.approx(22772.0)
    assert out["total_pnl_krw"] == pytest.approx(112963.4)
    assert out["fx_pnl_accuracy"] == "approximate"
```

- [ ] **Step 3: Run Toss tests to verify failure**

Run:

```bash
uv run pytest \
  tests/services/test_toss_live_order_ledger_service.py::test_update_reconcile_outcome_records_us_fx_fields \
  tests/mcp_server/tooling/test_toss_live_ledger.py::test_toss_us_buy_reconcile_captures_buy_fx_rate \
  tests/mcp_server/tooling/test_toss_live_ledger.py::test_toss_us_sell_reconcile_surfaces_fx_pnl \
  -q
```

Expected: FAIL because Toss update/reconcile does not accept or surface FX fields.

- [ ] **Step 4: Extend Toss ledger service**

In `app/services/toss_live_order_ledger_service.py`, extend `update_reconcile_outcome` signature with:

```python
        buy_fx_rate: Decimal | None = None,
        sell_fx_rate: Decimal | None = None,
        fx_pnl_krw: Decimal | None = None,
        security_pnl_usd: Decimal | None = None,
        security_pnl_krw: Decimal | None = None,
        total_pnl_krw: Decimal | None = None,
        fx_rate_source: str | None = None,
        fx_pnl_accuracy: str | None = None,
```

Then assign when values are not `None`:

```python
        if buy_fx_rate is not None:
            row.buy_fx_rate = buy_fx_rate
        if sell_fx_rate is not None:
            row.sell_fx_rate = sell_fx_rate
        if fx_pnl_krw is not None:
            row.fx_pnl_krw = fx_pnl_krw
        if security_pnl_usd is not None:
            row.security_pnl_usd = security_pnl_usd
        if security_pnl_krw is not None:
            row.security_pnl_krw = security_pnl_krw
        if total_pnl_krw is not None:
            row.total_pnl_krw = total_pnl_krw
        if fx_rate_source is not None:
            row.fx_rate_source = fx_rate_source
        if fx_pnl_accuracy is not None:
            row.fx_pnl_accuracy = fx_pnl_accuracy
```

- [ ] **Step 5: Wire Toss reconcile**

In `app/mcp_server/tooling/toss_live_ledger.py`, import:

```python
from app.mcp_server.tooling.fx_pnl import capture_reconcile_spot_fx
```

After `avg_price` is known and before write side effects:

```python
    fx_capture = None
    if row.market == "us":
        fx_capture = await capture_reconcile_spot_fx()
```

For US buy, pass captured values to `_create_trade_journal_for_buy`:

```python
            buy_fx_rate=float(fx_capture.rate)
            if fx_capture is not None and fx_capture.rate is not None
            else None,
            fx_rate_source=fx_capture.fx_rate_source if fx_capture is not None else None,
            fx_pnl_accuracy=fx_capture.fx_pnl_accuracy if fx_capture is not None else None,
```

Set response fields for buy when `fx_capture` is present:

```python
        if fx_capture is not None:
            base["buy_fx_rate"] = (
                float(fx_capture.rate) if fx_capture.rate is not None else None
            )
            base["fx_rate_source"] = fx_capture.fx_rate_source
            base["fx_pnl_accuracy"] = fx_capture.fx_pnl_accuracy
```

For US sell, pass captured sell FX to `_close_journals_on_sell`:

```python
            sell_fx_rate=float(fx_capture.rate)
            if fx_capture is not None and fx_capture.rate is not None
            else None,
            fx_rate_source=fx_capture.fx_rate_source if fx_capture is not None else None,
            fx_pnl_accuracy=fx_capture.fx_pnl_accuracy if fx_capture is not None else None,
```

Copy close result fields to `base`:

```python
        for key in (
            "journals_closed",
            "closed_journal_ids",
            "realized_pnl_pct",
            "realized_pnl_basis",
            "journal_pnl_pct",
            "buy_fx_rate",
            "sell_fx_rate",
            "fx_pnl_krw",
            "security_pnl_usd",
            "security_pnl_krw",
            "total_pnl_krw",
            "fx_rate_source",
            "fx_pnl_accuracy",
            "fx_unavailable_journal_ids",
        ):
            if key in close_result:
                base[key] = close_result[key]
```

Pass Decimal-converted FX fields into `update_reconcile_outcome`:

```python
            buy_fx_rate=Decimal(str(base["buy_fx_rate"]))
            if base.get("buy_fx_rate") is not None
            else None,
            sell_fx_rate=Decimal(str(base["sell_fx_rate"]))
            if base.get("sell_fx_rate") is not None
            else None,
            fx_pnl_krw=Decimal(str(base["fx_pnl_krw"]))
            if base.get("fx_pnl_krw") is not None
            else None,
            security_pnl_usd=Decimal(str(base["security_pnl_usd"]))
            if base.get("security_pnl_usd") is not None
            else None,
            security_pnl_krw=Decimal(str(base["security_pnl_krw"]))
            if base.get("security_pnl_krw") is not None
            else None,
            total_pnl_krw=Decimal(str(base["total_pnl_krw"]))
            if base.get("total_pnl_krw") is not None
            else None,
            fx_rate_source=base.get("fx_rate_source"),
            fx_pnl_accuracy=base.get("fx_pnl_accuracy"),
```

- [ ] **Step 6: Run Toss tests**

Run:

```bash
uv run pytest \
  tests/services/test_toss_live_order_ledger_service.py::test_update_reconcile_outcome_records_us_fx_fields \
  tests/mcp_server/tooling/test_toss_live_ledger.py::test_toss_us_buy_reconcile_captures_buy_fx_rate \
  tests/mcp_server/tooling/test_toss_live_ledger.py::test_toss_us_sell_reconcile_surfaces_fx_pnl \
  -q
```

Expected: PASS.

- [ ] **Step 7: Commit Toss reconcile**

```bash
git add app/mcp_server/tooling/toss_live_ledger.py app/services/toss_live_order_ledger_service.py tests/mcp_server/tooling/test_toss_live_ledger.py tests/services/test_toss_live_order_ledger_service.py
git commit -m "feat(ROB-568): capture Toss US reconcile FX PnL"
```

---

### Task 5: Generic KIS US Live Reconcile Capture

**Files:**
- Modify: `app/mcp_server/tooling/live_order_ledger.py`
- Modify: `tests/mcp_server/tooling/test_live_order_ledger.py`

- [ ] **Step 1: Add failing generic live ledger test**

Append to `tests/mcp_server/tooling/test_live_order_ledger.py`:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_live_reconcile_sell_surfaces_fx_pnl_for_kis():
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import live_order_ledger as ll
    from app.mcp_server.tooling.fx_pnl import FxRateCapture
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    lid = await ll._save_live_order_ledger(
        broker="kis",
        account_scope="kis_live",
        market="us",
        symbol="AAPL",
        exchange="NASD",
        market_symbol="AAPL",
        side="sell",
        order_kind="limit",
        quantity=2.0,
        price=130.0,
        amount=260.0,
        currency="USD",
        order_no="US-FX-SELL",
        order_time=None,
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response=None,
        reason=None,
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
    )
    row = await ll._load_live_ledger_row(lid)
    filled = FillEvidence(
        FillVerdict.FILLED, Decimal("2"), Decimal("130"), None, "filled", ""
    )
    close_result = {
        "journals_closed": 1,
        "journals_kept": 0,
        "closed_ids": [77],
        "total_pnl_pct": 30.0,
        "realized_pnl_basis": "journal_entry",
        "buy_fx_rate": 1389.33,
        "sell_fx_rate": 1503.19,
        "fx_pnl_krw": 22772.0,
        "security_pnl_usd": 60.0,
        "security_pnl_krw": 90191.4,
        "total_pnl_krw": 112963.4,
        "fx_rate_source": "reconcile_spot",
        "fx_pnl_accuracy": "approximate",
        "fx_unavailable_journal_ids": [],
    }

    class _Adapter:
        broker = "kis"
        fetch_evidence = AsyncMock(return_value=filled)

    with (
        patch.object(ll, "get_evidence_adapter", return_value=_Adapter()),
        patch.object(
            ll,
            "capture_reconcile_spot_fx",
            new=AsyncMock(
                return_value=FxRateCapture(
                    Decimal("1503.19"), "reconcile_spot", "approximate"
                )
            ),
        ),
        patch.object(ll, "_save_order_fill", new=AsyncMock(return_value=222)),
        patch.object(ll, "_close_journals_on_sell", new=AsyncMock(return_value=close_result)),
    ):
        out = await ll._reconcile_one_live_row(row, dry_run=False)

    assert out["fx_pnl_krw"] == pytest.approx(22772.0)
    assert out["total_pnl_krw"] == pytest.approx(112963.4)
    assert out["fx_pnl_accuracy"] == "approximate"
```

- [ ] **Step 2: Run generic live ledger test to verify failure**

Run:

```bash
uv run pytest tests/mcp_server/tooling/test_live_order_ledger.py::test_us_live_reconcile_sell_surfaces_fx_pnl_for_kis -q
```

Expected: FAIL because generic live reconcile does not capture or persist FX fields.

- [ ] **Step 3: Extend generic live ledger update**

In `app/mcp_server/tooling/live_order_ledger.py`, import:

```python
from app.mcp_server.tooling.fx_pnl import capture_reconcile_spot_fx
```

Extend `_update_live_ledger_outcome` signature with the same eight FX parameters used by Toss. Assign them to `row` when not `None`.

- [ ] **Step 4: Wire generic US reconcile**

Inside `_reconcile_one_live_row`, after `avg_price` is known:

```python
    fx_capture = None
    if row.market == "us":
        fx_capture = await capture_reconcile_spot_fx()
```

Pass `buy_fx_rate`, `fx_rate_source`, and `fx_pnl_accuracy` to `_create_trade_journal_for_buy` for US buy rows. Pass `sell_fx_rate`, `fx_rate_source`, and `fx_pnl_accuracy` to `_close_journals_on_sell` for US sell rows.

Copy the close result FX fields to `base`, then pass Decimal-converted fields to `_update_live_ledger_outcome`.

- [ ] **Step 5: Run generic live ledger tests**

Run:

```bash
uv run pytest \
  tests/mcp_server/tooling/test_live_order_ledger.py::test_us_live_reconcile_sell_surfaces_fx_pnl_for_kis \
  tests/mcp_server/tooling/test_live_order_ledger.py::test_reconcile_filled_sell_surfaces_journal_entry_basis \
  -q
```

Expected: PASS. The existing crypto/KR behavior should not gain FX values because only `row.market == "us"` captures spot FX.

- [ ] **Step 6: Commit generic live reconcile**

```bash
git add app/mcp_server/tooling/live_order_ledger.py tests/mcp_server/tooling/test_live_order_ledger.py
git commit -m "feat(ROB-568): capture KIS US reconcile FX PnL"
```

---

### Task 6: Manual Journal Override

**Files:**
- Modify: `app/mcp_server/tooling/trade_journal_tools.py`
- Modify: `app/mcp_server/tooling/trade_journal_registration.py`
- Modify: `tests/test_mcp_trade_journal.py`

- [ ] **Step 1: Add failing manual override tests**

Append to `tests/test_mcp_trade_journal.py`:

```python
@pytest.mark.asyncio
async def test_update_trade_journal_allows_manual_exact_fx_override():
    from app.mcp_server.tooling.trade_journal_tools import (
        save_trade_journal,
        update_trade_journal,
    )

    created = await save_trade_journal(
        symbol="AAPL",
        thesis="legacy import",
        entry_price=100.0,
        quantity=2.0,
        account="toss",
        account_type="live",
        status="active",
    )
    journal_id = created["data"]["id"]

    updated = await update_trade_journal(
        journal_id=journal_id,
        buy_fx_rate=1389.33,
        sell_fx_rate=1503.19,
        fx_pnl_krw=22772.0,
        security_pnl_usd=60.0,
        security_pnl_krw=90191.4,
        total_pnl_krw=112963.4,
        fx_rate_source="manual",
        fx_pnl_accuracy="exact",
    )

    assert updated["success"] is True
    assert updated["data"]["buy_fx_rate"] == pytest.approx(1389.33)
    assert updated["data"]["fx_pnl_krw"] == pytest.approx(22772.0)
    assert updated["data"]["fx_rate_source"] == "manual"
    assert updated["data"]["fx_pnl_accuracy"] == "exact"


@pytest.mark.asyncio
async def test_update_trade_journal_rejects_invalid_fx_labels():
    from app.mcp_server.tooling.trade_journal_tools import save_trade_journal, update_trade_journal

    created = await save_trade_journal(
        symbol="AAPL",
        thesis="legacy import",
        account="toss",
        account_type="live",
        status="active",
    )

    result = await update_trade_journal(
        journal_id=created["data"]["id"],
        fx_rate_source="broker_exact",
    )

    assert result["success"] is False
    assert "fx_rate_source" in result["error"]
```

- [ ] **Step 2: Run manual override tests to verify failure**

Run:

```bash
uv run pytest \
  tests/test_mcp_trade_journal.py::test_update_trade_journal_allows_manual_exact_fx_override \
  tests/test_mcp_trade_journal.py::test_update_trade_journal_rejects_invalid_fx_labels \
  -q
```

Expected: FAIL because `update_trade_journal` has no FX parameters.

- [ ] **Step 3: Serialize FX fields**

In `_serialize_journal`, add:

```python
        "buy_fx_rate": float(j.buy_fx_rate) if j.buy_fx_rate is not None else None,
        "sell_fx_rate": float(j.sell_fx_rate) if j.sell_fx_rate is not None else None,
        "fx_pnl_krw": float(j.fx_pnl_krw) if j.fx_pnl_krw is not None else None,
        "security_pnl_usd": float(j.security_pnl_usd) if j.security_pnl_usd is not None else None,
        "security_pnl_krw": float(j.security_pnl_krw) if j.security_pnl_krw is not None else None,
        "total_pnl_krw": float(j.total_pnl_krw) if j.total_pnl_krw is not None else None,
        "fx_rate_source": j.fx_rate_source,
        "fx_pnl_accuracy": j.fx_pnl_accuracy,
```

- [ ] **Step 4: Add label validation helper**

In `trade_journal_tools.py`, add constants:

```python
_VALID_FX_RATE_SOURCES = {"reconcile_spot", "manual", "unavailable"}
_VALID_FX_PNL_ACCURACIES = {"approximate", "exact", "unavailable"}
```

Add helper:

```python
def _validate_fx_labels(
    *, fx_rate_source: str | None, fx_pnl_accuracy: str | None
) -> str | None:
    if fx_rate_source is not None and fx_rate_source not in _VALID_FX_RATE_SOURCES:
        return "fx_rate_source must be one of: approximate source labels are reconcile_spot, manual, unavailable"
    if (
        fx_pnl_accuracy is not None
        and fx_pnl_accuracy not in _VALID_FX_PNL_ACCURACIES
    ):
        return "fx_pnl_accuracy must be one of: approximate, exact, unavailable"
    return None
```

- [ ] **Step 5: Extend save/update trade journal**

Add optional FX parameters to `save_trade_journal` and `update_trade_journal` signatures:

```python
    buy_fx_rate: float | None = None,
    sell_fx_rate: float | None = None,
    fx_pnl_krw: float | None = None,
    security_pnl_usd: float | None = None,
    security_pnl_krw: float | None = None,
    total_pnl_krw: float | None = None,
    fx_rate_source: str | None = None,
    fx_pnl_accuracy: str | None = None,
```

In each function, call `_validate_fx_labels(...)` before DB writes and return `{"success": False, "error": error}` when non-null.

In `save_trade_journal`, set the corresponding `TradeJournal` constructor fields with `Decimal(str(value))` for numeric FX values.

In `update_trade_journal`, assign each provided value:

```python
            if buy_fx_rate is not None:
                journal.buy_fx_rate = Decimal(str(buy_fx_rate))
            if sell_fx_rate is not None:
                journal.sell_fx_rate = Decimal(str(sell_fx_rate))
            if fx_pnl_krw is not None:
                journal.fx_pnl_krw = Decimal(str(fx_pnl_krw))
            if security_pnl_usd is not None:
                journal.security_pnl_usd = Decimal(str(security_pnl_usd))
            if security_pnl_krw is not None:
                journal.security_pnl_krw = Decimal(str(security_pnl_krw))
            if total_pnl_krw is not None:
                journal.total_pnl_krw = Decimal(str(total_pnl_krw))
            if fx_rate_source is not None:
                journal.fx_rate_source = fx_rate_source
            if fx_pnl_accuracy is not None:
                journal.fx_pnl_accuracy = fx_pnl_accuracy
```

- [ ] **Step 6: Update registration descriptions**

In `app/mcp_server/tooling/trade_journal_registration.py`, append this sentence to the `save_trade_journal` and `update_trade_journal` descriptions:

```python
"US FX fields buy_fx_rate/sell_fx_rate/fx_pnl_krw/security_pnl_usd/security_pnl_krw/total_pnl_krw plus fx_rate_source/fx_pnl_accuracy support manual exact backfill."
```

- [ ] **Step 7: Run manual override tests**

Run:

```bash
uv run pytest \
  tests/test_mcp_trade_journal.py::test_update_trade_journal_allows_manual_exact_fx_override \
  tests/test_mcp_trade_journal.py::test_update_trade_journal_rejects_invalid_fx_labels \
  -q
```

Expected: PASS.

- [ ] **Step 8: Commit manual override**

```bash
git add app/mcp_server/tooling/trade_journal_tools.py app/mcp_server/tooling/trade_journal_registration.py tests/test_mcp_trade_journal.py
git commit -m "feat(ROB-568): allow manual exact FX journal override"
```

---

### Task 7: Retrospective FX Fields

**Files:**
- Modify: `app/services/trade_journal/trade_retrospective_service.py`
- Modify: `app/mcp_server/tooling/trade_retrospective_tools.py`
- Modify: `tests/test_trade_retrospective_service.py`
- Modify: `tests/test_trade_retrospective_tools.py`
- Modify: `tests/test_trade_retrospective_aggregate.py`

- [ ] **Step 1: Add failing service tests**

Append to `tests/test_trade_retrospective_service.py`:

```python
@pytest.mark.asyncio
async def test_toss_live_account_mode_allowed(db_session: AsyncSession):
    _, row = await svc.save_retrospective(
        db_session,
        symbol="AAPL",
        instrument_type="equity_us",
        account_mode="toss_live",
        outcome="filled",
        realized_pnl=60.0,
    )
    await db_session.commit()
    assert row.account_mode == "toss_live"
    assert row.realized_pnl_currency == "USD"


@pytest.mark.asyncio
async def test_retrospective_derives_fx_fields_from_journal(db_session: AsyncSession):
    j = TradeJournal(
        symbol="AAPL",
        instrument_type="equity_us",
        side="buy",
        entry_price=Decimal("100"),
        quantity=Decimal("2"),
        thesis="t",
        account_type="live",
        account="toss",
        status="closed",
        exit_price=Decimal("130"),
        buy_fx_rate=Decimal("1389.33"),
        sell_fx_rate=Decimal("1503.19"),
        fx_pnl_krw=Decimal("22772.0000"),
        security_pnl_usd=Decimal("60.0000"),
        security_pnl_krw=Decimal("90191.4000"),
        total_pnl_krw=Decimal("112963.4000"),
        fx_rate_source="reconcile_spot",
        fx_pnl_accuracy="approximate",
    )
    db_session.add(j)
    await db_session.commit()
    await db_session.refresh(j)

    _, row = await svc.save_retrospective(
        db_session,
        symbol="AAPL",
        instrument_type="equity_us",
        account_mode="toss_live",
        outcome="filled",
        journal_id=j.id,
    )
    await db_session.commit()

    assert row.fx_pnl_krw == Decimal("22772.0000")
    assert row.security_pnl_usd == Decimal("60.0000")
    assert row.total_pnl_krw == Decimal("112963.4000")
    assert row.fx_rate_source == "reconcile_spot"
    assert row.fx_pnl_accuracy == "approximate"
```

- [ ] **Step 2: Add failing aggregate and MCP tests**

Append to `tests/test_trade_retrospective_aggregate.py`:

```python
@pytest.mark.asyncio
async def test_aggregate_sums_fx_and_total_krw(db_session: AsyncSession):
    await svc.save_retrospective(
        db_session,
        symbol="AAPL",
        instrument_type="equity_us",
        account_mode="toss_live",
        outcome="filled",
        strategy_key="A",
        realized_pnl=60.0,
        realized_pnl_currency="USD",
        fx_pnl_krw=22772.0,
        total_pnl_krw=112963.4,
    )
    await db_session.commit()

    result = await svc.build_retrospective_aggregate(db_session, group_by="strategy")
    group = result["groups"][0]
    assert group["fx_pnl_krw_sum"] == pytest.approx(22772.0)
    assert group["total_pnl_krw_sum"] == pytest.approx(112963.4)
```

Append to `tests/test_trade_retrospective_tools.py`:

```python
@pytest.mark.asyncio
async def test_save_retrospective_accepts_fx_fields():
    res = await save_trade_retrospective(
        symbol="AAPL",
        instrument_type="equity_us",
        account_mode="toss_live",
        outcome="filled",
        realized_pnl=60.0,
        buy_fx_rate=1389.33,
        sell_fx_rate=1503.19,
        fx_pnl_krw=22772.0,
        security_pnl_usd=60.0,
        security_pnl_krw=90191.4,
        total_pnl_krw=112963.4,
        fx_rate_source="manual",
        fx_pnl_accuracy="exact",
    )
    assert res["success"] is True
    assert res["data"]["fx_pnl_krw"] == pytest.approx(22772.0)
    assert res["data"]["fx_pnl_accuracy"] == "exact"
```

- [ ] **Step 3: Run retrospective tests to verify failure**

Run:

```bash
uv run pytest \
  tests/test_trade_retrospective_service.py::test_toss_live_account_mode_allowed \
  tests/test_trade_retrospective_service.py::test_retrospective_derives_fx_fields_from_journal \
  tests/test_trade_retrospective_aggregate.py::test_aggregate_sums_fx_and_total_krw \
  tests/test_trade_retrospective_tools.py::test_save_retrospective_accepts_fx_fields \
  -q
```

Expected: FAIL because `toss_live` and FX fields are not supported.

- [ ] **Step 4: Extend service validation and serializer**

In `trade_retrospective_service.py`, add `"toss_live"` to `_VALID_ACCOUNT_MODES`.

Extend `serialize_retrospective` with the same eight FX fields used by journals.

Add optional parameters to `save_retrospective` signature:

```python
    buy_fx_rate: float | None = None,
    sell_fx_rate: float | None = None,
    fx_pnl_krw: float | None = None,
    security_pnl_usd: float | None = None,
    security_pnl_krw: float | None = None,
    total_pnl_krw: float | None = None,
    fx_rate_source: str | None = None,
    fx_pnl_accuracy: str | None = None,
```

After `_derive_realized_pnl_from_journal`, load journal FX fields when `journal_id` is present and caller did not provide a value:

```python
    journal_fx: TradeJournal | None = None
    if journal_id is not None:
        journal_fx = (
            await db.execute(select(TradeJournal).where(TradeJournal.id == journal_id))
        ).scalar_one_or_none()

    if journal_fx is not None:
        if buy_fx_rate is None and journal_fx.buy_fx_rate is not None:
            buy_fx_rate = float(journal_fx.buy_fx_rate)
        if sell_fx_rate is None and journal_fx.sell_fx_rate is not None:
            sell_fx_rate = float(journal_fx.sell_fx_rate)
        if fx_pnl_krw is None and journal_fx.fx_pnl_krw is not None:
            fx_pnl_krw = float(journal_fx.fx_pnl_krw)
        if security_pnl_usd is None and journal_fx.security_pnl_usd is not None:
            security_pnl_usd = float(journal_fx.security_pnl_usd)
        if security_pnl_krw is None and journal_fx.security_pnl_krw is not None:
            security_pnl_krw = float(journal_fx.security_pnl_krw)
        if total_pnl_krw is None and journal_fx.total_pnl_krw is not None:
            total_pnl_krw = float(journal_fx.total_pnl_krw)
        fx_rate_source = fx_rate_source or journal_fx.fx_rate_source
        fx_pnl_accuracy = fx_pnl_accuracy or journal_fx.fx_pnl_accuracy
```

Add those fields to `payload` with `_to_decimal(...)` for numeric values.

- [ ] **Step 5: Extend aggregate**

Inside `build_retrospective_aggregate`, add sums:

```python
        fx_pnl_sum = sum(
            float(it.fx_pnl_krw) for it in items if it.fx_pnl_krw is not None
        )
        total_pnl_krw_sum = sum(
            float(it.total_pnl_krw) for it in items if it.total_pnl_krw is not None
        )
```

Add to each group:

```python
                "fx_pnl_krw_sum": fx_pnl_sum,
                "total_pnl_krw_sum": total_pnl_krw_sum,
```

- [ ] **Step 6: Extend MCP wrapper**

In `trade_retrospective_tools.py`, add the eight FX parameters to `save_trade_retrospective`, pass them through to `save_retrospective`, and rely on `serialize_retrospective` for output.

- [ ] **Step 7: Run retrospective tests**

Run:

```bash
uv run pytest \
  tests/test_trade_retrospective_service.py::test_toss_live_account_mode_allowed \
  tests/test_trade_retrospective_service.py::test_retrospective_derives_fx_fields_from_journal \
  tests/test_trade_retrospective_aggregate.py::test_aggregate_sums_fx_and_total_krw \
  tests/test_trade_retrospective_tools.py::test_save_retrospective_accepts_fx_fields \
  -q
```

Expected: PASS.

- [ ] **Step 8: Commit retrospective support**

```bash
git add app/services/trade_journal/trade_retrospective_service.py app/mcp_server/tooling/trade_retrospective_tools.py tests/test_trade_retrospective_service.py tests/test_trade_retrospective_tools.py tests/test_trade_retrospective_aggregate.py
git commit -m "feat(ROB-568): expose FX PnL in trade retrospectives"
```

---

### Task 8: Documentation And Tool Descriptions

**Files:**
- Modify: `app/mcp_server/README.md`
- Modify: `docs/runbooks/toss-live-order-reconcile.md`
- Modify: `tests/test_mcp_toss_order_variants.py`

- [ ] **Step 1: Add failing description test**

In `tests/test_mcp_toss_order_variants.py::test_toss_tool_descriptions_document_live_gates`, after the existing reconcile assertions, add:

```python
    assert "US FX PnL" in reconcile_desc
    assert "fx_pnl_accuracy" in reconcile_desc
```

- [ ] **Step 2: Run description test to verify failure**

Run:

```bash
uv run pytest tests/test_mcp_toss_order_variants.py::test_toss_tool_descriptions_document_live_gates -q
```

Expected: FAIL because `toss_reconcile_orders` description does not mention FX fields.

- [ ] **Step 3: Update Toss registration description**

In `app/mcp_server/tooling/orders_toss_variants.py`, extend the `toss_reconcile_orders` description with:

```python
"US FX PnL: US buy reconcile captures buy_fx_rate from reconcile-spot USD/KRW; US sell reconcile captures sell_fx_rate and returns fx_pnl_krw/security_pnl_usd/security_pnl_krw/total_pnl_krw with fx_rate_source and fx_pnl_accuracy."
```

- [ ] **Step 4: Update MCP README**

In `app/mcp_server/README.md`, extend the Toss accepted-only ledger bullet:

```markdown
- **US FX PnL split**: Toss order detail does not provide fill-time FX. For US rows only, `toss_reconcile_orders(dry_run=False)` captures USD/KRW through `exchange_rate_service` at reconcile time. Buy rows persist `buy_fx_rate`; sell rows persist `sell_fx_rate`, FIFO-attributed `fx_pnl_krw`, `security_pnl_usd`, `security_pnl_krw`, and `total_pnl_krw`. Automatic values are labelled `fx_rate_source="reconcile_spot"` and `fx_pnl_accuracy="approximate"`. Legacy lots with no buy FX keep FX PnL fields null until an operator backfills exact values through `update_trade_journal`.
```

- [ ] **Step 5: Update Toss runbook**

In `docs/runbooks/toss-live-order-reconcile.md`, add a section before Operational Hold:

```markdown
## US FX PnL Split

Toss `GET /orders/{orderId}` execution does not include fill-time FX fields. For US orders only, reconcile captures the current USD/KRW quote from `exchange_rate_service` when the fill is booked:

- buy reconcile stores `buy_fx_rate`;
- sell reconcile stores `sell_fx_rate`;
- closed FIFO journal lots store `security_pnl_usd`, `security_pnl_krw`, `fx_pnl_krw`, and `total_pnl_krw`;
- automatic values use `fx_rate_source='reconcile_spot'` and `fx_pnl_accuracy='approximate'`.

Legacy lots with no captured buy FX cannot produce automatic FX PnL. They remain `fx_pnl_accuracy='unavailable'` with null FX PnL fields until the operator supplies exact values through `update_trade_journal(..., fx_rate_source='manual', fx_pnl_accuracy='exact')`.

The split formula is:

```text
security_pnl_usd = sell_notional_usd - buy_notional_usd
security_pnl_krw = security_pnl_usd * sell_fx_rate
fx_pnl_krw = buy_notional_usd * (sell_fx_rate - buy_fx_rate)
total_pnl_krw = security_pnl_krw + fx_pnl_krw
```
```

- [ ] **Step 6: Run doc/description tests**

Run:

```bash
uv run pytest tests/test_mcp_toss_order_variants.py::test_toss_tool_descriptions_document_live_gates -q
```

Expected: PASS.

- [ ] **Step 7: Commit docs**

```bash
git add app/mcp_server/tooling/orders_toss_variants.py app/mcp_server/README.md docs/runbooks/toss-live-order-reconcile.md tests/test_mcp_toss_order_variants.py
git commit -m "docs(ROB-568): document US FX PnL reconcile contract"
```

---

### Task 9: Full Verification And Linear Hold

**Files:**
- No source edits expected unless verification exposes a failure.

- [ ] **Step 1: Run targeted test suite**

Run:

```bash
uv run pytest \
  tests/mcp_server/tooling/test_fx_pnl.py \
  tests/test_trade_journal_model.py \
  tests/test_rob538_toss_live_ledger_schema.py \
  tests/mcp_server/tooling/test_live_order_ledger.py \
  tests/mcp_server/tooling/test_toss_live_ledger.py \
  tests/services/test_toss_live_order_ledger_service.py \
  tests/test_mcp_trade_journal.py \
  tests/test_trade_retrospective_model.py \
  tests/test_trade_retrospective_service.py \
  tests/test_trade_retrospective_tools.py \
  tests/test_trade_retrospective_aggregate.py \
  tests/test_mcp_toss_order_variants.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run lint and Alembic checks**

Run:

```bash
make lint
uv run alembic heads
```

Expected: lint PASS; Alembic shows one head: `20260615_rob568_us_fx_pnl (head)`.

- [ ] **Step 3: Run broader test gate**

Run:

```bash
make test-unit
```

Expected: PASS. If DB-backed tests are excluded by the unit target, also run:

```bash
uv run pytest -m "not live" -q
```

Expected: PASS or only known unrelated failures documented with exact failing test names and stack traces.

- [ ] **Step 4: Inspect diff for live-order boundaries**

Run:

```bash
git diff --stat
git diff -- app/mcp_server/tooling/toss_live_ledger.py app/mcp_server/tooling/live_order_ledger.py app/mcp_server/tooling/order_journal.py app/services/toss_live_order_ledger_service.py app/models app/services/trade_journal alembic/versions/20260615_rob568_us_fx_pnl.py
```

Expected:

- no live order placement confirmation gates are relaxed;
- no mutation retry behavior is changed;
- no secrets or credentials are added;
- FX capture happens only after broker fill evidence is already classified and only for `market == "us"`;
- automatic FX values are labelled `reconcile_spot` and `approximate`.

- [ ] **Step 5: Add Linear comment**

Post this ROB-568 comment:

```markdown
Implementation plan applies `high_risk_change` + `needs_stronger_model_review` because ROB-568 changes DB schema and live US reconcile bookkeeping.

Final design:
- Toss order-detail execution has no fill-time FX, so automatic FX uses reconcile-time USD/KRW spot from `exchange_rate_service`.
- Automatic values are labelled `fx_rate_source="reconcile_spot"` and `fx_pnl_accuracy="approximate"`.
- US buy reconcile captures `buy_fx_rate`; US sell reconcile captures `sell_fx_rate` and FIFO-attributed `fx_pnl_krw`.
- Legacy lots without buy FX remain unavailable/null until manual exact override through `update_trade_journal`.
- KIS US generic `live_reconcile_orders` follows the same US-only path; KR domestic reconcile is unchanged.

Holding merge/deploy/live operational use under `hold_for_final_review` until stronger-model/CTO review clears schema rollback and live bookkeeping assumptions.
```

- [ ] **Step 6: Commit final verification note if project convention wants one**

If a verification note is added, create `docs/superpowers/verifications/2026-06-15-rob-568-us-fx-pnl.md` with exact commands and outcomes, then commit:

```bash
git add docs/superpowers/verifications/2026-06-15-rob-568-us-fx-pnl.md
git commit -m "docs(ROB-568): record FX PnL verification"
```

If no verification note is added, do not create a commit for this step.

---

## Self-Review

- Spec coverage: Covers Toss US, KIS US generic live reconcile, manual exact override, legacy unavailable handling, migration after ROB-569, retrospective exposure, docs, tests, and Linear hold.
- Placeholder scan: No unresolved placeholders, incomplete implementation slots, or undefined output names are left in task steps.
- Type consistency: FX field names are identical across models, migration, services, MCP serializers, and tests.
- Scope control: This does not alter live order placement gates, Toss mutation retry behavior, KR domestic reconcile behavior, or execution policy.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-15-rob-568-us-fx-pnl-plan.md`. Two execution options:

**1. Subagent-Driven (recommended)** - Dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?

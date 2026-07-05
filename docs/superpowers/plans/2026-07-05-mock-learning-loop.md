# Mock Learning Loop (provenance spine) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Wire ROB-703 paper trades into the learning corpus — a deterministic `correlation_id` spine minted at place-time carries order → fill → journal → forecast → retrospective, and filled paper trades auto-surface as pending retrospectives (with a `stop_loss` suggestion), so mock trading accumulates queryable, calibratable decision data.

**Architecture:** Additive on the ROB-703 paper sim. New: a pure `correlation_id` builder + one additive migration (4 nullable columns on each paper table + `paper` account_mode + `stop_loss` trigger_type) + place-time provenance (draft paper TradeJournal + `price_target` forecast) + a fill bridge (carry correlation_id, activate/close the journal) + a paper source branch in `build_retrospective_pending`. Reuses `_create_trade_journal_for_buy`, `save_forecast`, `PaperTradingService`. No new stores, no real-broker path.

**Tech Stack:** Python 3.13, SQLAlchemy async + Postgres (schemas `paper` + `review`), alembic, FastMCP, pytest (markers unit/integration).

## Global Constraints

- **Pure sim:** touches no real broker/Upbit; reads live market data + writes only `paper`/`review` tables. ROB-501: no in-process LLM.
- **Lesson stays authored:** automation captures provenance + surfaces outcomes; `trigger_type`/`root_cause`/lesson are written by the human/Claude session, never auto-drafted. `probability` for forecasts is **caller-supplied** (the MCP/session), not invented server-side.
- **account_mode value = `paper`** (subsystem is multi-market; `upbit_paper` too narrow).
- **Names stay in lock-step:** the trigger_type CHECK (`ck_trade_retrospectives_trigger_type`, review.py), `VALID_TRIGGER_TYPES` (app/schemas/trade_retrospective.py), and the migration must all list the same set; the account_mode CHECK (`account_mode`, review.py) and `_VALID_ACCOUNT_MODES` (service) must match.
- **correlation_id must be salted with KST trade-day + rung** (mirror ROB-653 P6-B) so a re-placed/duplicate order does not collide (the retrospective `correlation_id` is UNIQUE and coverage dedups on it).
- **TradeJournal field reality:** it has NO `realized_pnl` (PnL lives in `pnl_pct`), NO `strategy_key` (column is `strategy`), NO `created_by`. `correlation_id` IS a nullable Text column but `_create_trade_journal_for_buy` does not accept it (set it on the returned object).
- Deferred (NOT this plan): resting sell-STOP order type; real-account wiring; auto-drafted retrospectives; a realized-PnL forecast resolver.
- Lint: `uv run ruff check app/ tests/` + `ruff format --check` + `uv run ty check app/ --error-on-warning`. Tests: `uv run --all-groups pytest <file> -v --no-cov`. Every commit ends with the standard Co-Authored-By + Claude-Session trailer.

## File Structure

- `app/services/paper_correlation.py` — **create**: pure `paper_correlation_id(...)` spine builder.
- `alembic/versions/20260705_rob705_paper_provenance.py` — **create**: additive migration (4 cols × 2 paper tables + 2 CHECK DROP+ADD).
- `app/models/paper_trading.py` — **modify**: add `correlation_id`/`journal_id`/`artifact_uuid`/`forecast_id` to `PaperTrade` + `PaperPendingOrder`.
- `app/models/review.py` — **modify**: `account_mode` CHECK (+`paper`), `ck_trade_retrospectives_trigger_type` CHECK (+`stop_loss`).
- `app/schemas/trade_retrospective.py` — **modify**: `VALID_TRIGGER_TYPES` (+`stop_loss`).
- `app/services/trade_journal/trade_retrospective_service.py` — **modify**: `_VALID_ACCOUNT_MODES` (+`paper`); `_pending_entry` (spine-id + trigger override); paper source branch in `build_retrospective_pending`.
- `app/services/paper_limit_order_service.py` — **modify**: place mints correlation_id + draft journal + optional forecast; reconcile carries correlation_id + activates/closes journal.
- `app/mcp_server/tooling/paper_limit_order_handler.py` — **modify**: add `strategy`/`target_price`/`stop_loss`/`probability`/`review_date`/`artifact_uuid` params to `paper_place_limit_order`.
- Tests: `tests/services/test_paper_correlation.py`, `tests/services/test_paper_provenance.py`, `tests/services/test_paper_retrospective_pending.py`.

---

## Task 1: Correlation-id spine (pure)

**Files:** Create `app/services/paper_correlation.py`; Test `tests/services/test_paper_correlation.py`

**Interfaces — Produces:** `paper_correlation_id(*, account_id: int, symbol: str, side: str, limit_price: Decimal, quantity: Decimal, kst_trade_day: str, rung: int = 0) -> str` → `paper:{account_id}:{sha16}` where `sha16 = sha256(canonical).hexdigest()[:16]` over a canonical string that includes the KST trade-day + rung salt (ROB-653 P6-B mirror). Deterministic; same inputs → same id; different trade-day OR rung → different id.

- [ ] **Step 1: Write failing tests.**
```python
# tests/services/test_paper_correlation.py
from decimal import Decimal
import pytest
from app.services.paper_correlation import paper_correlation_id

def _id(**kw):
    base = dict(account_id=1, symbol="KRW-BTC", side="buy",
               limit_price=Decimal("94000000"), quantity=Decimal("0.001"),
               kst_trade_day="2026-07-05", rung=0)
    base.update(kw)
    return paper_correlation_id(**base)

@pytest.mark.unit
def test_deterministic_same_inputs():
    assert _id() == _id()
    assert _id().startswith("paper:1:")

@pytest.mark.unit
def test_trade_day_salt_changes_id():
    assert _id(kst_trade_day="2026-07-05") != _id(kst_trade_day="2026-07-06")

@pytest.mark.unit
def test_rung_salt_changes_id():
    assert _id(rung=0) != _id(rung=1)

@pytest.mark.unit
def test_symbol_side_price_qty_change_id():
    assert _id(symbol="KRW-ETH") != _id()
    assert _id(side="sell") != _id()
    assert _id(limit_price=Decimal("94000001")) != _id()
    assert _id(quantity=Decimal("0.002")) != _id()
```

- [ ] **Step 2: Run — expect FAIL.** `uv run --all-groups pytest tests/services/test_paper_correlation.py -v --no-cov`

- [ ] **Step 3: Implement.**
```python
# app/services/paper_correlation.py
"""Deterministic correlation-id spine for the paper learning loop (ROB-705).

Mirrors ROB-653 P6-B idempotency keying: the canonical string includes the KST
trade-day and a rung discriminator so a re-placed order (after cancel) or two
identical ladder rungs do NOT collide on one id. Collision would be silent —
review.trade_retrospectives.correlation_id is UNIQUE and pending-coverage
dedups on it, so one retrospective would "cover" two distinct orders.
Pure: no I/O, no LLM.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal


def paper_correlation_id(
    *,
    account_id: int,
    symbol: str,
    side: str,
    limit_price: Decimal,
    quantity: Decimal,
    kst_trade_day: str,
    rung: int = 0,
) -> str:
    # canonical decision fields | KST trade-day | rung  (ROB-653 P6-B shape)
    canonical = "|".join(
        (
            symbol.upper(),
            side.lower(),
            format(limit_price, "f"),
            format(quantity, "f"),
            kst_trade_day,
            str(rung),
        )
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"paper:{account_id}:{digest}"
```

- [ ] **Step 4: Run — expect PASS. Lint.** `uv run --all-groups pytest tests/services/test_paper_correlation.py -v --no-cov` ; `uv run ruff check app/services/paper_correlation.py tests/services/test_paper_correlation.py && uv run ty check app/ --error-on-warning`

- [ ] **Step 5: Commit.** `git add app/services/paper_correlation.py tests/services/test_paper_correlation.py && git commit -m "feat(ROB-705): deterministic paper correlation-id spine (trade-day+rung salted)"`

---

## Task 2: Migration + model columns + CHECK edits

**Files:** Create `alembic/versions/20260705_rob705_paper_provenance.py`; Modify `app/models/paper_trading.py`, `app/models/review.py`, `app/schemas/trade_retrospective.py`, `app/services/trade_journal/trade_retrospective_service.py`

**Interfaces — Produces:** `PaperTrade`/`PaperPendingOrder` gain nullable `correlation_id: str|None`, `journal_id: int|None`, `artifact_uuid: str|None`, `forecast_id: str|None`. `account_mode` CHECK + `_VALID_ACCOUNT_MODES` accept `'paper'`. `ck_trade_retrospectives_trigger_type` CHECK + `VALID_TRIGGER_TYPES` accept `'stop_loss'`.

- [ ] **Step 1: Add the 4 columns to both paper models.** In `app/models/paper_trading.py`, add to `PaperTrade` (after `realized_pnl`, before `executed_at`) AND to `PaperPendingOrder` (after `thesis`), the identical block:
```python
    correlation_id: Mapped[str | None] = mapped_column(Text)
    journal_id: Mapped[int | None] = mapped_column(BigInteger)
    artifact_uuid: Mapped[str | None] = mapped_column(Text)
    forecast_id: Mapped[str | None] = mapped_column(Text)
```
(`Text`, `BigInteger` are already imported in that module. No `__table_args__` change — plain nullable columns, no CHECK, no FK, keeping it additive/loose-coupled.)

- [ ] **Step 2: Edit the two review CHECKs.** In `app/models/review.py`:
  - account_mode CHECK (~966): change the IN-list to `"account_mode IN ('kis_mock','kiwoom_mock','kis_live','toss_live','alpaca_paper','upbit_live','paper')"` (keep `name="account_mode"`).
  - trigger_type CHECK (~991): append `'stop_loss'` so it reads `...'stale_evidence','guardrail_block','stop_loss'` (keep `name="ck_trade_retrospectives_trigger_type"`).

- [ ] **Step 3: Edit the two Python allow-sets.**
  - `app/schemas/trade_retrospective.py` `VALID_TRIGGER_TYPES` frozenset: add `"stop_loss",`.
  - `app/services/trade_journal/trade_retrospective_service.py` `_VALID_ACCOUNT_MODES`: add `"paper",`.
  (Do NOT put `paper` in VALID_TRIGGER_TYPES or `stop_loss` in account modes.)

- [ ] **Step 4: Write the migration** (down_revision = current head `20260704_rob703`; confirm via `uv run alembic heads`). Template A = rob647 add_column loop; the CHECK DROP+recreate uses `op.drop_constraint`/`op.create_check_constraint`.
```python
# alembic/versions/20260705_rob705_paper_provenance.py
"""ROB-705 paper provenance: correlation cols + paper account_mode + stop_loss trigger."""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "20260705_rob705"
down_revision: str | Sequence[str] | None = "20260704_rob703"
branch_labels = None
depends_on = None

_PAPER_COLS = (
    ("correlation_id", sa.Text()),
    ("journal_id", sa.BigInteger()),
    ("artifact_uuid", sa.Text()),
    ("forecast_id", sa.Text()),
)
_ACCOUNT_MODES_NEW = "account_mode IN ('kis_mock','kiwoom_mock','kis_live','toss_live','alpaca_paper','upbit_live','paper')"
_ACCOUNT_MODES_OLD = "account_mode IN ('kis_mock','kiwoom_mock','kis_live','toss_live','alpaca_paper','upbit_live')"
_TRIGGER_NEW = ("trigger_type IS NULL OR trigger_type IN ("
    "'fill','partial_fill','rejected_order','cancelled','expired',"
    "'thesis_change','policy_violation','stale_evidence','guardrail_block','stop_loss')")
_TRIGGER_OLD = ("trigger_type IS NULL OR trigger_type IN ("
    "'fill','partial_fill','rejected_order','cancelled','expired',"
    "'thesis_change','policy_violation','stale_evidence','guardrail_block')")


def upgrade() -> None:
    for tbl in ("paper_trades", "paper_pending_orders"):
        for name, col_type in _PAPER_COLS:
            op.add_column(tbl, sa.Column(name, col_type, nullable=True), schema="paper")
    op.drop_constraint("account_mode", "trade_retrospectives", schema="review", type_="check")
    op.create_check_constraint("account_mode", "trade_retrospectives", _ACCOUNT_MODES_NEW, schema="review")
    op.drop_constraint("ck_trade_retrospectives_trigger_type", "trade_retrospectives", schema="review", type_="check")
    op.create_check_constraint("ck_trade_retrospectives_trigger_type", "trade_retrospectives", _TRIGGER_NEW, schema="review")


def downgrade() -> None:
    op.drop_constraint("ck_trade_retrospectives_trigger_type", "trade_retrospectives", schema="review", type_="check")
    op.create_check_constraint("ck_trade_retrospectives_trigger_type", "trade_retrospectives", _TRIGGER_OLD, schema="review")
    op.drop_constraint("account_mode", "trade_retrospectives", schema="review", type_="check")
    op.create_check_constraint("account_mode", "trade_retrospectives", _ACCOUNT_MODES_OLD, schema="review")
    for tbl in ("paper_trades", "paper_pending_orders"):
        for name, _ in _PAPER_COLS:
            op.drop_column(tbl, name, schema="paper")
```
(Match the exact old IN-list strings to whatever `alembic heads`/the model shows, so `drop_constraint`+recreate is a clean swap.)

- [ ] **Step 5: Verify + commit.** `uv run python -c "from app.models.paper_trading import PaperTrade, PaperPendingOrder; print(PaperTrade.correlation_id, PaperPendingOrder.forecast_id)"` (imports OK); the test DB auto-creates the new columns via `create_all` on the models. `uv run ruff check app/ && uv run ty check app/ --error-on-warning`. Commit: `git add app/models/paper_trading.py app/models/review.py app/schemas/trade_retrospective.py app/services/trade_journal/trade_retrospective_service.py alembic/versions/20260705_rob705_paper_provenance.py && git commit -m "feat(ROB-705): paper provenance columns + paper account_mode + stop_loss trigger_type"`

---

## Task 3: Place-time provenance (correlation_id + draft journal + forecast)

**Files:** Modify `app/services/paper_limit_order_service.py`, `app/mcp_server/tooling/paper_limit_order_handler.py`; Test `tests/services/test_paper_provenance.py`

**Interfaces:**
- Consumes: `paper_correlation_id` (Task 1), the new columns (Task 2), `app.mcp_server.tooling.order_journal._create_trade_journal_for_buy`, `app.services.trade_journal.forecast_service.save_forecast`, `app.core.timezone`.
- Produces: `place_limit_order` accepts `strategy: str|None=None, target_price: Decimal|None=None, stop_loss: Decimal|None=None, probability: float|None=None, review_date: str|None=None, artifact_uuid: str|None=None`; it stamps `correlation_id` on the `PaperPendingOrder`, writes a draft paper `TradeJournal` (buy + thesis), stores `journal_id`, and (when `probability` + `target_price` + `review_date` present) creates a `price_target` forecast carrying the same `correlation_id` and stamps `forecast_id`.

- [ ] **Step 1: Write failing integration tests** (`@pytest.mark.asyncio`, `db_session`; monkeypatch `save_forecast` + `_create_trade_journal_for_buy` seams are avoidable — call the real ones against the test DB).
```python
# tests/services/test_paper_provenance.py
from decimal import Decimal
from typing import Any
import pytest
from app.services.paper_trading_service import PaperTradingService
from app.services.paper_limit_order_service import PaperLimitOrderService
from app.models.paper_trading import PaperPendingOrder
from sqlalchemy import select

@pytest.mark.asyncio
async def test_place_stamps_correlation_and_journal(db_session: Any) -> None:
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(name="rob705-prov", initial_capital_krw=Decimal("1000000"))
    svc = PaperLimitOrderService(db_session)
    out = await svc.place_limit_order(
        account_id=acct.id, symbol="KRW-BTC", side="buy",
        limit_price=Decimal("90000000"), amount=Decimal("100000"),
        thesis="support bounce", strategy="support_ladder",
        target_price=Decimal("100000000"), stop_loss=Decimal("85000000"),
        probability=0.6, review_date="2026-07-15",
    )
    assert out["success"], out
    row = (await db_session.execute(
        select(PaperPendingOrder).where(PaperPendingOrder.id == out["order_id"])
    )).scalar_one()
    assert row.correlation_id and row.correlation_id.startswith(f"paper:{acct.id}:")
    assert row.journal_id is not None          # draft journal linked
    assert row.forecast_id is not None         # forecast linked
```

- [ ] **Step 2: Run — expect FAIL** (params/columns unset). `uv run --all-groups pytest tests/services/test_paper_provenance.py -v --no-cov`

- [ ] **Step 3: Implement.** In `paper_limit_order_service.py`:
  1. Add imports: `from app.services.paper_correlation import paper_correlation_id`; `from app.core.timezone import now_kst`; `from app.mcp_server.tooling.order_journal import _create_trade_journal_for_buy`; `from app.services.trade_journal.forecast_service import save_forecast`.
  2. Add the new keyword params to `place_limit_order` (all default None): `strategy`, `target_price`, `stop_loss`, `probability`, `review_date`, `artifact_uuid`.
  3. After the snapped price + qty are resolved and BEFORE building the `PaperPendingOrder`, mint the id:
     ```python
     kst_day = now_kst().strftime("%Y-%m-%d")
     corr_id = paper_correlation_id(
         account_id=account_id, symbol=resolved_symbol, side=side_norm,
         limit_price=snapped_price, quantity=qty, kst_trade_day=kst_day, rung=0,
     )
     ```
     Set `correlation_id=corr_id` on the `PaperPendingOrder(...)` constructor.
  4. After the order is persisted (`await self.db.flush()`), for `side_norm == "buy"` and `thesis`:
     ```python
     journal = await _create_trade_journal_for_buy(
         symbol=resolved_symbol, market_type="crypto",
         preview={"price": snapped_price, "quantity": qty, "estimated_value": quantize_money(qty * snapped_price)},
         thesis=thesis, strategy=(strategy or ""),
         target_price=(float(target_price) if target_price is not None else None),
         stop_loss=(float(stop_loss) if stop_loss is not None else None),
         min_hold_days=None, notes=None, indicators_snapshot=None,
         account_type="paper", account=account.name,
     )
     journal.correlation_id = corr_id          # helper doesn't accept it; set on the returned obj
     order.journal_id = journal.id
     ```
     (`account` is the object from `self.pts.get_account(account_id)`; ensure it's loaded before this block. `_create_trade_journal_for_buy` returns the `TradeJournal` — verify the return; if it returns a result dict, read `.get("journal")`/`["id"]` and adapt.)
  5. When `probability is not None and target_price is not None and review_date`:
     ```python
     direction = "at_or_above"  # buy profit target sits above entry -> price must RISE to it (at_or_below is trivially true -> corrupts Brier)
     _, fc = await save_forecast(
         self.db, created_by="paper_sim", symbol=resolved_symbol, instrument_type="crypto",
         forecast_target={"kind": "price_target", "direction": direction, "target_price": float(target_price)},
         probability=float(probability), review_date=review_date, correlation_id=corr_id,
         horizon=None, model_label=None, session_label="paper_place", artifact_uuid=artifact_uuid,
     )
     order.forecast_id = str(getattr(fc, "forecast_id", getattr(fc, "id", "")))
     ```
     (`save_forecast` returns `(action, row)`; use the row's id column — confirm the attr name from `forecast_service.py`.)
  6. Set `order.artifact_uuid = artifact_uuid` when present. Keep the existing `await self.db.commit()`.

  In `paper_limit_order_handler.py`: add the same optional params (`strategy: str|None=None, target_price: float|None=None, stop_loss: float|None=None, probability: float|None=None, review_date: str|None=None, artifact_uuid: str|None=None`) to the `paper_place_limit_order` tool signature and thread them into the `place_limit_order` call (converting float→Decimal for target_price/stop_loss).

- [ ] **Step 4: Run — expect PASS. Lint + ty.** `uv run --all-groups pytest tests/services/test_paper_provenance.py -v --no-cov` ; `uv run ruff check app/ tests/ && uv run ty check app/ --error-on-warning`

- [ ] **Step 5: Commit.** `git add -A && git commit -m "feat(ROB-705): place-time paper provenance (correlation_id + draft journal + price_target forecast)"`

---

## Task 4: Fill bridge (carry correlation_id + activate/close journal)

**Files:** Modify `app/services/paper_limit_order_service.py`; extend `tests/services/test_paper_provenance.py`

**Interfaces:** `reconcile_pending_orders` copies `PaperPendingOrder.{correlation_id,journal_id,artifact_uuid,forecast_id}` onto the booked `PaperTrade`, and activates the draft journal on a buy fill (or closes it on a sell fill so `pnl_pct` is set).

- [ ] **Step 1: Write the failing test** (reuse the Task-3 tz-naive candle helper pattern from `tests/services/test_paper_limit_order_service.py`).
```python
@pytest.mark.asyncio
async def test_fill_carries_correlation_to_paper_trade(db_session: Any, monkeypatch: Any) -> None:
    import datetime as dt
    from app.core.timezone import now_kst
    from app.models.paper_trading import PaperTrade
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(name="rob705-fill", initial_capital_krw=Decimal("1000000"))
    svc = PaperLimitOrderService(db_session)
    out = await svc.place_limit_order(
        account_id=acct.id, symbol="KRW-BTC", side="buy",
        limit_price=Decimal("94400000"), amount=Decimal("100000"), thesis="t")
    corr = out.get("correlation_id") or (await svc.get_pending_order(account_id=acct.id, order_id=out["order_id"]))["...
    ts = now_kst().replace(tzinfo=None) + dt.timedelta(minutes=1)
    class _C:  # tz-naive candle, low crosses
        low = Decimal("94000000"); high = Decimal("95000000"); timestamp = ts
    async def _bars(symbol, market, period, count, end=None): return [_C()]
    monkeypatch.setattr("app.services.paper_limit_order_service.get_ohlcv", _bars)
    await svc.reconcile_pending_orders(account_id=acct.id)
    tr = (await db_session.execute(select(PaperTrade).where(PaperTrade.account_id == acct.id))).scalar_one()
    assert tr.correlation_id and tr.correlation_id.startswith("paper:")
    assert tr.journal_id is not None
```
(Fix the `corr` line to read `row.correlation_id` via `get_pending_order`/a direct select — the point is the PaperTrade ends up carrying the spine id + journal_id.)

- [ ] **Step 2: Run — expect FAIL.** `uv run --all-groups pytest tests/services/test_paper_provenance.py -v --no-cov -k carries_correlation`

- [ ] **Step 3: Implement.** In `reconcile_pending_orders`, inside the per-order fill block (after `execute_order` books the `PaperTrade`, in the same commit): fetch the just-booked trade (the existing `_latest_trade_id` already finds it) and set `trade.correlation_id = order.correlation_id`, `trade.journal_id = order.journal_id`, `trade.artifact_uuid = order.artifact_uuid`, `trade.forecast_id = order.forecast_id`. Then bridge the journal:
  - buy fill: `await _activate_paper_journal(self.db, symbol=order.symbol, account_name=account.name)` (import from `app.mcp_server.tooling.paper_order_handler`; matches the newest draft paper journal for that symbol+account and flips it to active — reuse verbatim).
  - sell fill: `await _close_journals_on_sell(symbol=order.symbol, sell_quantity=float(order.quantity), sell_price=float(fill_price), exit_reason=(order.thesis or "paper resting-limit fill"), account_type="paper", account=account.name)` (sets `pnl_pct` on the matched journal).
  Wrap the journal bridge in a local `try/except` that logs + continues (a missing draft journal must not fail the fill). Keep the id-based loop + per-order commit from ROB-703's polish.

- [ ] **Step 4: Run — expect PASS. Full paper suite + lint.** `uv run --all-groups pytest tests/services/test_paper_provenance.py tests/services/test_paper_limit_order_service.py -v --no-cov ; uv run ruff check app/ tests/ && uv run ty check app/ --error-on-warning`

- [ ] **Step 5: Commit.** `git add -A && git commit -m "feat(ROB-705): fill bridge — carry correlation_id to PaperTrade + activate/close journal"`

---

## Task 5: Pending-scan paper source branch + stop_loss suggestion

**Files:** Modify `app/services/trade_journal/trade_retrospective_service.py`; Test `tests/services/test_paper_retrospective_pending.py`

**Interfaces:** `build_retrospective_pending` gains a 4th source: filled `PaperTrade` rows in the KST window not yet covered, each `suggested_correlation_id = PaperTrade.correlation_id` (the spine, NOT `{ledger}:{ref}`) and `suggested_trigger_type = 'stop_loss'` when `side == 'sell' AND realized_pnl < 0`.

- [ ] **Step 1: Write failing test.**
```python
# tests/services/test_paper_retrospective_pending.py
from decimal import Decimal
from typing import Any
import pytest
from app.services.paper_trading_service import PaperTradingService
from app.services.trade_journal.trade_retrospective_service import build_retrospective_pending
from app.core.timezone import now_kst

@pytest.mark.asyncio
async def test_paper_fill_surfaces_as_pending_with_stop_loss_suggestion(db_session: Any) -> None:
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(name="rob705-pend", initial_capital_krw=Decimal("100000000"))
    # a loss-making sell paper trade (stop-loss): buy then sell below cost
    async def _p(s, i): return Decimal("50000000")
    import pytest as _pt
    from unittest.mock import patch
    with patch.object(pts, "_fetch_current_price", _p):
        await pts.execute_order(account_id=acct.id, symbol="KRW-BTC", side="buy", order_type="market", quantity=Decimal("0.01"))
    async def _p2(s, i): return Decimal("45000000")
    with patch.object(pts, "_fetch_current_price", _p2):
        tr = await pts.execute_order(account_id=acct.id, symbol="KRW-BTC", side="sell", order_type="market", quantity=Decimal("0.01"))
    today = now_kst().strftime("%Y-%m-%d")
    pending = await build_retrospective_pending(db_session, from_date=today, to_date=today, include_cancelled=False)
    paper = [e for e in pending if e.get("account_mode") == "paper" and e.get("symbol") == "KRW-BTC"]
    assert paper, pending
    sells = [e for e in paper if e.get("side") == "sell"]
    assert sells and sells[0].get("suggested_trigger_type") == "stop_loss"
```
(Adjust to the real `build_retrospective_pending` return-entry keys — the paper branch must set `account_mode="paper"`, `side`, `suggested_trigger_type`.)

- [ ] **Step 2: Run — expect FAIL** (no paper branch). `uv run --all-groups pytest tests/services/test_paper_retrospective_pending.py -v --no-cov`

- [ ] **Step 3: Implement.**
  1. Import: `from app.models.paper_trading import PaperTrade` (in the ledger-import block).
  2. Extend `_pending_entry` (line 861) with two optional kwargs (keeps the 3 existing callers unchanged): `suggested_correlation_id: str | None = None`, `suggested_trigger_type: str | None = None`. In the returned dict, use `suggested_correlation_id or f"{ledger}:{ref}"` for the correlation key and add `"suggested_trigger_type": suggested_trigger_type`.
  3. Add branch 4 in `build_retrospective_pending` after the Toss branch (~line 1026, before the `excluded_cancelled` block). Query filled paper trades in the KST window (paper_trades has NO status column — every row is a fill, so no `.in_()` status filter, window only), cap at `_PENDING_LEDGER_FETCH_CAP`:
     ```python
     paper_rows = (await db.execute(
         select(PaperTrade)
         .where(PaperTrade.executed_at >= window_start, PaperTrade.executed_at <= window_end)
         .order_by(PaperTrade.executed_at.desc())
         .limit(_PENDING_LEDGER_FETCH_CAP)
     )).scalars().all()
     for r in paper_rows:
         trig = "stop_loss" if (r.side == "sell" and r.realized_pnl is not None and r.realized_pnl < 0) else None
         entry = _pending_entry(
             ledger="paper_trades", account_mode="paper",
             market=("crypto" if r.instrument_type.value == "crypto" else r.instrument_type.value.replace("equity_", "")),
             instrument_type=r.instrument_type.value, symbol=r.symbol, side=r.side,
             status="filled", order_ref=(r.correlation_id or f"paper_trade:{r.id}"),
             report_item_uuid=None, trade_date=r.executed_at, row_id=r.id,
             suggested_correlation_id=(r.correlation_id or f"paper_trade:{r.id}"),
             suggested_trigger_type=trig,
         )
         if not _is_covered(entry, covered_cids, covered_uuids):
             pending.append(entry)
     ```
     (`window_start`/`window_end`/`covered_cids`/`covered_uuids`/`pending` are the same locals the live branches use — read the exact names from the function body and match them; the market-string mapping should follow whatever the live branches emit.)

- [ ] **Step 4: Run — expect PASS. Full retrospective + paper suite + lint.** `uv run --all-groups pytest tests/services/test_paper_retrospective_pending.py tests/services/test_paper_provenance.py -v --no-cov` + the existing retrospective-service tests ; `uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/ && uv run ty check app/ --error-on-warning`

- [ ] **Step 5: Commit.** `git add -A && git commit -m "feat(ROB-705): paper source branch in retrospective-pending + stop_loss suggestion"`

---

## Operator follow-up (not code)
After merge + `alembic upgrade head`: re-run the ROB-703 trial with the new params (thesis/target/stop/probability/review_date on `paper_place_limit_order`), reconcile, then `trade_retrospective_pending` → author the first full-provenance stop-loss retrospective (citing journal + forecast + artifact) → `get_retrospective_aggregate(group_by="account_mode")` / `trigger_type` to see paper stop-loss outcomes.

## Roadmap (out of scope)
Resting sell-STOP order type; real-account provenance wiring (same spine); auto-drafted retrospective skeletons; a realized-PnL forecast resolver; unified `get_decision_corpus` view.

## Self-Review
- Spec coverage: correlation spine→T1; migration+cols+CHECKs+account_mode+trigger_type→T2; place-time journal+forecast+correlation→T3; fill bridge→T4; pending paper branch+stop_loss→T5. ✓
- Grounded realities honored: TradeJournal has no realized_pnl/strategy_key/created_by (use pnl_pct/strategy; set correlation_id on the returned obj); VALID_TRIGGER_TYPES lives in app/schemas/ not app/services/; paper_trades has no status column (window-only filter); named CHECKs need DROP+ADD.
- Open items flagged for the implementer to confirm from source: `_create_trade_journal_for_buy` return shape (obj vs dict); `save_forecast` return row id attr; the exact local names in `build_retrospective_pending` (window_start/covered_cids/pending) + the live branches' market-string form.
- Deferrals explicit (STOP type, real wiring, auto-draft, PnL resolver). No creep.

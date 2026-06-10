# ROB-478~481 Execution Ledger Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore `/invest/my?tab=sellHistory` realized P/L by backfilling missing historical buy fills into `review.execution_ledger`, then seeding residual opening lots with honest `manual_import` provenance.

**Architecture:** Keep the existing FIFO read model in `app/services/execution_ledger/query_service.py`; do not change matcher semantics or add a migration. PR1 adds KIS bounded historical windows and truncation guards, PR2 replaces Upbit page paging with documented 7-day closed-order windows and dedup, and PR3 adds a dry-run-first seed CLI for broker average-cost opening lots that remain outside retention. All writes continue through `ExecutionLedgerRepository.upsert_fill`, default to dry-run, and require `EXECUTION_LEDGER_COMMIT_ENABLED=true` for commit mode.

**Tech Stack:** Python 3.13, SQLAlchemy async sessions, Pydantic `ExecutionLedgerUpsert`, KIS/Upbit read-only broker clients, argparse CLIs, pytest/pytest-asyncio, Ruff, ty.

**Linear Scope:** Parent `ROB-478`; child PRs `ROB-479`, `ROB-480`, `ROB-481`.

**Risk Lane:** Apply `high_risk_change` + `needs_stronger_model_review` to all four Linear issues. Implementation may merge only after stronger-model/CTO review. Operator commit runs remain blocked until a reviewer explicitly clears the dry-run evidence.

**External API Note:** Upbit Korean docs for `GET /v1/orders/closed` currently specify `start_time`/`end_time`, maximum 7-day query windows, and `limit` up to 1000; `page` is not part of the documented query contract. Reference: `https://docs.upbit.com/kr/reference/list-closed-orders`.

---

## File Structure

**Modify:**
- `app/services/execution_ledger/query_service.py` — no behavior change; tests pin FIFO fail-closed, venue isolation, multi-lot matching, and gross P/L semantics.
- `app/services/execution_ledger/reconciler.py` — accept explicit `start_at`/`end_at` and `max_pages`, record the actual window, and pass the window through to the filled-orders fetcher.
- `app/services/n8n_filled_orders_service.py` — accept explicit windows; KIS uses `YYYYMMDD` pass-through; Upbit uses sliding 7-day windows.
- `scripts/reconcile_execution_ledger.py` — add `--start-date`, `--end-date`, and `--max-pages` while preserving `--window-hours`.
- `app/services/brokers/kis/domestic_orders.py` — expose `max_pages`, raise on remaining continuation cursor after the cap.
- `app/services/brokers/kis/overseas_orders.py` — same truncation behavior as domestic orders.
- `app/services/brokers/upbit/orders.py` — replace `page` contract with `start_time`/`end_time` and `limit <= 1000`.
- `app/core/config.py` — add authenticated Upbit order endpoints to `DEFAULT_UPBIT_API_RATE_LIMITS`.
- `app/services/brokers/upbit/client.py` — preserve `avg_buy_price_modified` in account parsing.

**Create:**
- `app/services/execution_ledger/opening_lots.py` — pure opening-lot planning, ledger net calculation, and `ExecutionLedgerUpsert` construction.
- `scripts/seed_execution_ledger_opening_lots.py` — dry-run-first operator CLI for `source='manual_import'` opening lots.
- `tests/services/execution_ledger/test_query_service_profit.py` — FIFO characterization tests.
- `tests/scripts/test_reconcile_execution_ledger_cli.py` — CLI argument parsing and pass-through tests.
- `tests/services/execution_ledger/test_opening_lots.py` — pure seed calculation tests.
- `tests/scripts/test_seed_execution_ledger_opening_lots_cli.py` — seed CLI dry-run/commit gate tests.

**Existing Test Files To Extend:**
- `tests/services/execution_ledger/test_reconciler.py`
- `tests/test_n8n_trade_review.py`
- `tests/test_kis_domestic_orders_retry.py`
- `tests/test_kis_overseas_orders_retry.py`
- `tests/test_upbit_orders.py`
- `tests/services/execution_ledger/test_no_broker_mutation.py`

---

### Task 0: Linear Risk Metadata And Review Hold

**Files:**
- No repo files.
- Linear: `ROB-478`, `ROB-479`, `ROB-480`, `ROB-481`

- [ ] **Step 1: Apply labels without removing existing labels**

Use the Linear connector:

```text
ROB-478 labels: Improvement, high_risk_change, needs_stronger_model_review
ROB-479 labels: Improvement, high_risk_change, needs_stronger_model_review
ROB-480 labels: Bug, high_risk_change, needs_stronger_model_review
ROB-481 labels: Improvement, high_risk_change, needs_stronger_model_review
```

- [ ] **Step 2: Add parent issue review-hold comment**

Create this comment on `ROB-478`:

```markdown
Applying `high_risk_change` + `needs_stronger_model_review` for ROB-478~481: this work changes historical execution-ledger backfill and manual opening-lot seeding, which writes operational trading data. Code may be implemented, but no merge, deploy, production commit backfill, or manual_import seed execution should occur until stronger-model/CTO review clears the dry-run evidence and operator runbook.
```

- [ ] **Step 3: Confirm**

Expected: all four issues still retain their original `Improvement`/`Bug` label plus the two review labels. No issue status change is required before implementation starts.

---

### Task 1: FIFO Sell-History Characterization Tests

**Files:**
- Create: `tests/services/execution_ledger/test_query_service_profit.py`
- Modify: `app/services/execution_ledger/query_service.py` only if a characterization test exposes a mismatch with the current intended behavior.

- [ ] **Step 1: Write characterization tests**

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.schemas.execution_ledger import ExecutionLedgerRead
from app.services.execution_ledger.query_service import _annotate_realized_profit


def _item(
    *,
    side: str,
    qty: str,
    price: str,
    filled_at: datetime,
    order_id: str,
    fill_seq: int = 0,
    broker: str = "kis",
    account_mode: str = "live",
    venue: str = "krx",
    instrument_type: str = "equity_kr",
    symbol: str = "005930",
    currency: str = "KRW",
    fee_amount: str | None = None,
) -> ExecutionLedgerRead:
    quantity = Decimal(qty)
    unit_price = Decimal(price)
    return ExecutionLedgerRead(
        id=None,
        broker=broker,
        account_mode=account_mode,
        venue=venue,
        instrument_type=instrument_type,
        symbol=symbol,
        raw_symbol=symbol,
        side=side,
        broker_order_id=order_id,
        fill_seq=fill_seq,
        filled_qty=quantity,
        filled_price=unit_price,
        filled_notional=quantity * unit_price,
        fee_amount=Decimal(fee_amount) if fee_amount is not None else None,
        fee_currency=currency if fee_amount is not None else None,
        filled_at=filled_at,
        currency=currency,
        source="reconciler",
    )


def test_annotate_realized_profit_uses_multilot_fifo() -> None:
    base = datetime(2026, 5, 1, tzinfo=UTC)
    buy_a = _item(side="buy", qty="5", price="100", filled_at=base, order_id="buy-a")
    buy_b = _item(
        side="buy",
        qty="5",
        price="120",
        filled_at=base + timedelta(days=1),
        order_id="buy-b",
    )
    sell = _item(
        side="sell",
        qty="7",
        price="150",
        filled_at=base + timedelta(days=2),
        order_id="sell-a",
    )

    annotated = _annotate_realized_profit([sell], [buy_a, buy_b, sell])

    assert annotated[0].cost_basis_notional == Decimal("740")
    assert annotated[0].realized_profit == Decimal("310")
    assert annotated[0].realized_profit_rate == Decimal("41.89189189189189189189189189")


def test_annotate_realized_profit_keeps_null_when_partially_uncovered() -> None:
    base = datetime(2026, 5, 1, tzinfo=UTC)
    buy = _item(side="buy", qty="5", price="100", filled_at=base, order_id="buy-a")
    sell = _item(
        side="sell",
        qty="7",
        price="150",
        filled_at=base + timedelta(days=1),
        order_id="sell-a",
    )

    annotated = _annotate_realized_profit([sell], [buy, sell])

    assert annotated[0].cost_basis_notional is None
    assert annotated[0].realized_profit is None
    assert annotated[0].realized_profit_rate is None


def test_annotate_realized_profit_isolates_venue_in_match_key() -> None:
    base = datetime(2026, 5, 1, tzinfo=UTC)
    nasd_buy = _item(
        side="buy",
        qty="10",
        price="100",
        filled_at=base,
        order_id="buy-a",
        venue="NASD",
        instrument_type="equity_us",
        symbol="AAPL",
        currency="USD",
    )
    nyse_sell = _item(
        side="sell",
        qty="1",
        price="150",
        filled_at=base + timedelta(days=1),
        order_id="sell-a",
        venue="NYSE",
        instrument_type="equity_us",
        symbol="AAPL",
        currency="USD",
    )

    annotated = _annotate_realized_profit([nyse_sell], [nasd_buy, nyse_sell])

    assert annotated[0].realized_profit is None


def test_annotate_realized_profit_remains_gross_and_ignores_fees() -> None:
    base = datetime(2026, 5, 1, tzinfo=UTC)
    buy = _item(
        side="buy",
        qty="1",
        price="100",
        filled_at=base,
        order_id="buy-a",
        fee_amount="10",
    )
    sell = _item(
        side="sell",
        qty="1",
        price="130",
        filled_at=base + timedelta(days=1),
        order_id="sell-a",
        fee_amount="10",
    )

    annotated = _annotate_realized_profit([sell], [buy, sell])

    assert annotated[0].cost_basis_notional == Decimal("100")
    assert annotated[0].realized_profit == Decimal("30")
    assert annotated[0].realized_profit_rate == Decimal("30.0")
```

- [ ] **Step 2: Run characterization tests**

Run: `uv run pytest tests/services/execution_ledger/test_query_service_profit.py -v`

Expected: PASS. If a test fails, update only `app/services/execution_ledger/query_service.py` to preserve the issue-stated behavior: fail closed on uncovered sells, use the 6-tuple match key, FIFO lots, and gross P/L.

- [ ] **Step 3: Commit**

```bash
git add tests/services/execution_ledger/test_query_service_profit.py app/services/execution_ledger/query_service.py
git commit -m "test(ROB-479): pin sell history FIFO realized profit semantics"
```

---

### Task 2: Explicit Historical Window Contract

**Files:**
- Modify: `app/services/execution_ledger/reconciler.py`
- Modify: `app/services/n8n_filled_orders_service.py`
- Modify: `scripts/reconcile_execution_ledger.py`
- Modify: `tests/services/execution_ledger/test_reconciler.py`
- Modify: `tests/test_n8n_trade_review.py`
- Create: `tests/scripts/test_reconcile_execution_ledger_cli.py`

- [ ] **Step 1: Add failing reconciler window test**

Append to `tests/services/execution_ledger/test_reconciler.py`:

```python
@pytest.mark.asyncio
async def test_reconciler_passes_explicit_window_to_fetcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.execution_ledger.reconciler.settings",
        SimpleNamespace(EXECUTION_LEDGER_COMMIT_ENABLED=False),
    )
    captured: dict[str, object] = {}

    async def fetcher(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return {"orders": []}

    start_at = datetime(2026, 2, 1, tzinfo=UTC)
    end_at = datetime(2026, 2, 8, tzinfo=UTC)
    repo = FakeRepo(status="inserted")

    await ExecutionLedgerReconciler(repo, fetcher=fetcher).run(
        "kis",
        start_at=start_at,
        end_at=end_at,
        max_pages=25,
        dry_run=True,
    )

    assert captured["start_at"] == start_at
    assert captured["end_at"] == end_at
    assert captured["max_pages"] == 25
    assert repo.runs[0].window_start == start_at
    assert repo.runs[0].window_end == end_at
```

- [ ] **Step 2: Add failing CLI parse test**

```python
# tests/scripts/test_reconcile_execution_ledger_cli.py
from __future__ import annotations

from datetime import UTC, datetime

import scripts.reconcile_execution_ledger as cli


def test_parse_args_accepts_explicit_date_window(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "reconcile_execution_ledger.py",
            "--broker",
            "kis",
            "--start-date",
            "2026-02-01",
            "--end-date",
            "2026-02-08",
            "--max-pages",
            "25",
        ],
    )

    args = cli.parse_args()
    start_at, end_at = cli.resolve_window_args(args)

    assert start_at == datetime(2026, 2, 1, 0, 0, tzinfo=UTC)
    assert end_at == datetime(2026, 2, 8, 23, 59, 59, 999999, tzinfo=UTC)
    assert args.max_pages == 25
```

- [ ] **Step 3: Verify tests fail before implementation**

Run:

```bash
uv run pytest \
  tests/services/execution_ledger/test_reconciler.py::test_reconciler_passes_explicit_window_to_fetcher \
  tests/scripts/test_reconcile_execution_ledger_cli.py::test_parse_args_accepts_explicit_date_window \
  -v
```

Expected: FAIL because `run()` has no `start_at`/`end_at`/`max_pages` parameters and `resolve_window_args()` does not exist.

- [ ] **Step 4: Implement reconciler window pass-through**

Add a complete window resolver near the top of `app/services/execution_ledger/reconciler.py`:

```python
def _resolve_run_window(
    *,
    window_hours: int = 24,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    window_end = end_at or now
    window_start = start_at or (window_end - timedelta(hours=window_hours))
    if window_start >= window_end:
        raise ValueError("start_at must be before end_at")
    return window_start, window_end
```

Update `ExecutionLedgerReconciler.run` signature:

```python
async def run(
    self,
    broker: Broker,
    *,
    window_hours: int = 24,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    max_pages: int = 100,
    dry_run: bool = True,
) -> ReconcileDiff:
```

Replace the current `now`/`window_start`/`window_end` calculation with:

```python
window_start, window_end = _resolve_run_window(
    window_hours=window_hours,
    start_at=start_at,
    end_at=end_at,
)
```

Replace the current `_fetch_normalized` call with:

```python
fills = await self._fetch_normalized(
    broker,
    window_hours=window_hours,
    start_at=window_start,
    end_at=window_end,
    max_pages=max_pages,
    source_run_id=run_id,
)
```

```python
async def _fetch_normalized(
    self,
    broker: Broker,
    *,
    window_hours: int,
    start_at: datetime,
    end_at: datetime,
    max_pages: int,
    source_run_id: uuid.UUID,
) -> list[ExecutionLedgerUpsert]:
    days = max(1, int(((end_at - start_at).total_seconds() + 86399) / 86400))
    markets = "crypto" if broker == "upbit" else "kr,us"
    result = await self.fetcher(
        days=days,
        markets=markets,
        min_amount=0,
        include_indicators=False,
        start_at=start_at,
        end_at=end_at,
        max_pages=max_pages,
    )
    rows = result.get("orders") or result.get("items") or []
    return [
        to_execution_ledger_upsert(row, broker=broker, source_run_id=source_run_id)
        for row in rows
    ]
```

- [ ] **Step 5: Implement filled-orders pass-through**

In `app/services/n8n_filled_orders_service.py`, add parameters to `fetch_filled_orders`, `_fetch_kis_domestic_filled`, `_fetch_kis_overseas_filled`, and `_fetch_upbit_filled`:

```python
def _resolve_kst_window(
    *,
    days: int,
    start_at: datetime | None,
    end_at: datetime | None,
) -> tuple[datetime, datetime]:
    resolved_end = (end_at.astimezone(KST) if end_at else now_kst())
    resolved_start = (
        start_at.astimezone(KST)
        if start_at
        else resolved_end - timedelta(days=days)
    )
    if resolved_start >= resolved_end:
        raise ValueError("start_at must be before end_at")
    return resolved_start, resolved_end
```

KIS callers must use the resolved dates:

```python
start_kst, end_kst = _resolve_kst_window(days=days, start_at=start_at, end_at=end_at)
raw_orders = await kis.inquire_daily_order_domestic(
    start_date=start_kst.strftime("%Y%m%d"),
    end_date=end_kst.strftime("%Y%m%d"),
    stock_code="",
    side="00",
    max_pages=max_pages,
)
```

Do the same for `inquire_daily_order_overseas`.

- [ ] **Step 6: Implement CLI args**

In `scripts/reconcile_execution_ledger.py`:

```python
from datetime import UTC, datetime, time


def _parse_cli_date(value: str, *, end_of_day: bool) -> datetime:
    raw = value.strip()
    fmt = "%Y%m%d" if len(raw) == 8 and raw.isdigit() else "%Y-%m-%d"
    day = datetime.strptime(raw, fmt).date()
    boundary = time.max if end_of_day else time.min
    return datetime.combine(day, boundary, tzinfo=UTC)


def resolve_window_args(args: argparse.Namespace) -> tuple[datetime | None, datetime | None]:
    if args.start_date is None and args.end_date is None:
        return None, None
    if args.start_date is None or args.end_date is None:
        raise ValueError("--start-date and --end-date must be provided together")
    start_at = _parse_cli_date(args.start_date, end_of_day=False)
    end_at = _parse_cli_date(args.end_date, end_of_day=True)
    if start_at >= end_at:
        raise ValueError("--start-date must be before --end-date")
    return start_at, end_at
```

Add parser args:

```python
parser.add_argument("--start-date", help="UTC date YYYY-MM-DD or YYYYMMDD")
parser.add_argument("--end-date", help="UTC date YYYY-MM-DD or YYYYMMDD")
parser.add_argument("--max-pages", type=int, default=100)
```

Call:

```python
start_at, end_at = resolve_window_args(args)
diff = await reconciler.run(
    args.broker,
    window_hours=args.window_hours,
    start_at=start_at,
    end_at=end_at,
    max_pages=args.max_pages,
    dry_run=dry_run,
)
```

- [ ] **Step 7: Run tests**

Run:

```bash
uv run pytest \
  tests/services/execution_ledger/test_reconciler.py \
  tests/scripts/test_reconcile_execution_ledger_cli.py \
  tests/test_n8n_trade_review.py \
  -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add \
  app/services/execution_ledger/reconciler.py \
  app/services/n8n_filled_orders_service.py \
  scripts/reconcile_execution_ledger.py \
  tests/services/execution_ledger/test_reconciler.py \
  tests/test_n8n_trade_review.py \
  tests/scripts/test_reconcile_execution_ledger_cli.py
git commit -m "feat(ROB-479): support explicit execution ledger backfill windows"
```

---

### Task 3: KIS Pagination Cap And Truncation Guard

**Files:**
- Modify: `app/services/brokers/kis/domestic_orders.py`
- Modify: `app/services/brokers/kis/overseas_orders.py`
- Modify: `tests/test_kis_domestic_orders_retry.py`
- Modify: `tests/test_kis_overseas_orders_retry.py`

- [ ] **Step 1: Add domestic truncation test**

Append to `tests/test_kis_domestic_orders_retry.py`:

```python
    @pytest.mark.asyncio
    async def test_raises_when_domestic_history_reaches_max_pages_with_cursor(
        self, _mock_domestic_orders
    ):
        instance, parent = _mock_domestic_orders
        parent._request_with_rate_limit = AsyncMock(
            side_effect=[
                {
                    "rt_cd": "0",
                    "output1": [{"ord_no": "001", "pdno": "005930"}],
                    "ctx_area_fk100": "FK2",
                    "ctx_area_nk100": "NK2",
                },
                {
                    "rt_cd": "0",
                    "output1": [{"ord_no": "002", "pdno": "005930"}],
                    "ctx_area_fk100": "FK3",
                    "ctx_area_nk100": "NK3",
                },
            ]
        )

        with pytest.raises(RuntimeError, match="domestic daily order history truncated"):
            await instance.inquire_daily_order_domestic(
                start_date="20260201",
                end_date="20260208",
                max_pages=2,
            )
```

- [ ] **Step 2: Add overseas truncation test**

Append to `tests/test_kis_overseas_orders_retry.py`:

```python
    @pytest.mark.asyncio
    async def test_raises_when_overseas_history_reaches_max_pages_with_cursor(
        self, _mock_overseas_orders
    ):
        instance, parent = _mock_overseas_orders
        parent._request_with_rate_limit = AsyncMock(
            side_effect=[
                {
                    "rt_cd": "0",
                    "output1": [{"odno": "001", "pdno": "AAPL"}],
                    "ctx_area_fk200": "FK2",
                    "ctx_area_nk200": "NK2",
                },
                {
                    "rt_cd": "0",
                    "output1": [{"odno": "002", "pdno": "AAPL"}],
                    "ctx_area_fk200": "FK3",
                    "ctx_area_nk200": "NK3",
                },
            ]
        )

        with pytest.raises(RuntimeError, match="overseas daily order history truncated"):
            await instance.inquire_daily_order_overseas(
                start_date="20260201",
                end_date="20260208",
                max_pages=2,
            )
```

- [ ] **Step 3: Verify tests fail**

Run:

```bash
uv run pytest \
  tests/test_kis_domestic_orders_retry.py::TestDomesticOrdersTransientRetry::test_raises_when_domestic_history_reaches_max_pages_with_cursor \
  tests/test_kis_overseas_orders_retry.py::TestOverseasOrdersTransientRetry::test_raises_when_overseas_history_reaches_max_pages_with_cursor \
  -v
```

Expected: FAIL because KIS helpers do not accept `max_pages`.

- [ ] **Step 4: Implement domestic max_pages parameter**

Change the domestic method signature:

```python
async def inquire_daily_order_domestic(
    self,
    start_date: str,
    end_date: str,
    stock_code: str = "",
    side: str = "00",
    order_number: str = "",
    is_mock: bool = False,
    max_pages: int = 100,
) -> list[dict]:
```

Replace the local assignment with:

```python
page_limit = max(1, int(max_pages))
truncated = False
```

Change the loop header and cap handling:

```python
while page <= page_limit:
    # Keep the existing request, retry/error handling, order extraction,
    # all_orders.extend(orders), logging, and cursor extraction above this block.
    if not new_ctx_area_nk100 or new_ctx_area_nk100 == ctx_area_nk100:
        logging.info("마지막 페이지 도달 (연속조회 키 없음 또는 동일)")
        break

    ctx_area_fk100 = new_ctx_area_fk100
    ctx_area_nk100 = new_ctx_area_nk100
    tr_cont = "N"
    page += 1
    if page > page_limit:
        truncated = True
        break
    await asyncio.sleep(0.1)

if truncated:
    raise RuntimeError(
        "KIS domestic daily order history truncated "
        f"at max_pages={page_limit} for {start_date}~{end_date}"
    )
```

- [ ] **Step 5: Implement overseas max_pages parameter**

Mirror Step 4 in `inquire_daily_order_overseas`, with `ctx_area_fk200`/`ctx_area_nk200` and this error:

```python
raise RuntimeError(
    "KIS overseas daily order history truncated "
    f"at max_pages={page_limit} for {start_date}~{end_date}"
)
```

- [ ] **Step 6: Run KIS tests**

Run:

```bash
uv run pytest \
  tests/test_kis_domestic_orders_retry.py \
  tests/test_kis_overseas_orders_retry.py \
  -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add \
  app/services/brokers/kis/domestic_orders.py \
  app/services/brokers/kis/overseas_orders.py \
  tests/test_kis_domestic_orders_retry.py \
  tests/test_kis_overseas_orders_retry.py
git commit -m "fix(ROB-479): fail loudly on truncated KIS order backfills"
```

---

### Task 4: Upbit Closed-Orders Window Pager And Dedup

**Files:**
- Modify: `app/services/brokers/upbit/orders.py`
- Modify: `app/core/config.py`
- Modify: `app/services/n8n_filled_orders_service.py`
- Modify: `tests/test_upbit_orders.py`
- Modify: `tests/test_n8n_trade_review.py`

- [ ] **Step 1: Replace Upbit request-contract test**

Replace `tests/test_upbit_orders.py::test_closed_orders_passes_pagination_and_state_filters` with:

```python
    @pytest.mark.asyncio
    async def test_closed_orders_passes_time_window_and_state_filters(self, monkeypatch):
        from datetime import UTC, datetime

        from app.services.brokers.upbit import orders

        request = AsyncMock(return_value=[])
        monkeypatch.setattr(orders._client, "_request_with_auth", request)

        result = await orders.fetch_closed_orders(
            market="KRW-BTC",
            limit=1500,
            states=["done"],
            order_by="asc",
            start_time=datetime(2026, 2, 1, 0, 0, tzinfo=UTC),
            end_time=datetime(2026, 2, 7, 0, 0, tzinfo=UTC),
        )

        assert result == []
        request.assert_awaited_once()
        method, url = request.await_args.args
        assert method == "GET"
        assert url.endswith("/orders/closed")
        assert request.await_args.kwargs["query_params"] == {
            "states[]": ["done"],
            "limit": 1000,
            "order_by": "asc",
            "market": "KRW-BTC",
            "start_time": "2026-02-01T00:00:00+00:00",
            "end_time": "2026-02-07T00:00:00+00:00",
        }
```

- [ ] **Step 2: Add Upbit cancel-page regression test**

Append to `TestFilledOrdersService` in `tests/test_n8n_trade_review.py`:

```python
    @pytest.mark.asyncio
    async def test_fetch_upbit_filled_does_not_stop_on_zero_fill_cancel_window(self):
        from datetime import datetime

        from app.core.timezone import KST
        from app.services.n8n_filled_orders_service import _fetch_upbit_filled

        cancel_only = [
            {
                "uuid": "cancel-zero",
                "side": "bid",
                "price": "1000",
                "state": "cancel",
                "market": "KRW-XRP",
                "executed_volume": "0",
                "paid_fee": "0",
                "created_at": "2026-03-20T10:00:00+09:00",
            }
        ]
        real_fill = [
            {
                "uuid": "real-fill",
                "side": "bid",
                "price": "1000",
                "state": "done",
                "market": "KRW-XRP",
                "executed_volume": "5",
                "paid_fee": "2.5",
                "created_at": "2026-03-18T10:00:00+09:00",
            }
        ]
        fixed_now = datetime(2026, 3, 21, 0, 0, tzinfo=KST)

        with (
            patch(
                "app.services.n8n_filled_orders_service.upbit_service.fetch_closed_orders",
                new_callable=AsyncMock,
                side_effect=[cancel_only, real_fill],
            ),
            patch(
                "app.services.n8n_filled_orders_service.now_kst",
                return_value=fixed_now,
            ),
        ):
            orders, errors = await _fetch_upbit_filled(days=14)

        assert errors == []
        assert [order["order_id"] for order in orders] == ["real-fill"]
```

- [ ] **Step 3: Add Upbit UUID dedup test**

Append to `TestFilledOrdersService`:

```python
    @pytest.mark.asyncio
    async def test_fetch_upbit_filled_dedups_order_uuid_across_windows(self):
        from datetime import datetime

        from app.core.timezone import KST
        from app.services.n8n_filled_orders_service import _fetch_upbit_filled

        duplicate = {
            "uuid": "dup-fill",
            "side": "bid",
            "price": "1000",
            "state": "done",
            "market": "KRW-XRP",
            "executed_volume": "5",
            "paid_fee": "2.5",
            "created_at": "2026-03-18T10:00:00+09:00",
        }
        fixed_now = datetime(2026, 3, 21, 0, 0, tzinfo=KST)

        with (
            patch(
                "app.services.n8n_filled_orders_service.upbit_service.fetch_closed_orders",
                new_callable=AsyncMock,
                side_effect=[[duplicate], [duplicate]],
            ),
            patch(
                "app.services.n8n_filled_orders_service.now_kst",
                return_value=fixed_now,
            ),
        ):
            orders, errors = await _fetch_upbit_filled(days=14)

        assert errors == []
        assert [order["order_id"] for order in orders] == ["dup-fill"]
```

- [ ] **Step 4: Verify tests fail**

Run:

```bash
uv run pytest \
  tests/test_upbit_orders.py \
  tests/test_n8n_trade_review.py::TestFilledOrdersService::test_fetch_upbit_filled_does_not_stop_on_zero_fill_cancel_window \
  tests/test_n8n_trade_review.py::TestFilledOrdersService::test_fetch_upbit_filled_dedups_order_uuid_across_windows \
  -v
```

Expected: FAIL because current Upbit helper still sends `page` and the service breaks on pages with no appendable fills.

- [ ] **Step 5: Update Upbit closed-orders helper**

In `app/services/brokers/upbit/orders.py`:

```python
from datetime import datetime


def _format_upbit_time(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        raise ValueError("Upbit time parameter must not be empty")
    return text
```

Replace `fetch_closed_orders` with:

```python
async def fetch_closed_orders(
    market: str | None = None,
    limit: int = 100,
    states: list[str] | None = None,
    order_by: str = "desc",
    start_time: datetime | str | None = None,
    end_time: datetime | str | None = None,
) -> list[dict[str, Any]]:
    url = f"{_client.UPBIT_REST}/orders/closed"
    capped_limit = max(1, min(int(limit), 1000))
    normalized_states = states or ["done", "cancel"]
    params: dict[str, Any] = {
        "states[]": normalized_states,
        "limit": capped_limit,
        "order_by": order_by,
    }
    if market:
        params["market"] = market
    if start_time is not None:
        params["start_time"] = _format_upbit_time(start_time)
    if end_time is not None:
        params["end_time"] = _format_upbit_time(end_time)

    return await _client._request_with_auth("GET", url, query_params=params)
```

- [ ] **Step 6: Add rate limits**

In `app/core/config.py`:

```python
DEFAULT_UPBIT_API_RATE_LIMITS: ApiRateLimitMap = {
    "GET /v1/accounts": {"rate": 30, "period": 1.0},
    "GET /v1/order": {"rate": 30, "period": 1.0},
    "GET /v1/orders/closed": {"rate": 30, "period": 1.0},
    "GET /v1/ticker": {"rate": 10, "period": 1.0},
}
```

- [ ] **Step 7: Implement Upbit window crawl**

In `app/services/n8n_filled_orders_service.py`:

```python
_UPBIT_CLOSED_ORDERS_WINDOW = timedelta(days=7)
_UPBIT_CLOSED_ORDERS_LIMIT = 1000


def _iter_upbit_windows(
    start_at: datetime, end_at: datetime
) -> list[tuple[datetime, datetime]]:
    windows: list[tuple[datetime, datetime]] = []
    cursor_end = end_at
    while cursor_end > start_at:
        cursor_start = max(start_at, cursor_end - _UPBIT_CLOSED_ORDERS_WINDOW)
        windows.append((cursor_start, cursor_end))
        cursor_end = cursor_start
    return windows


async def _fetch_upbit_closed_window(
    start_at: datetime,
    end_at: datetime,
) -> list[dict[str, Any]]:
    rows = await upbit_service.fetch_closed_orders(
        market=None,
        limit=_UPBIT_CLOSED_ORDERS_LIMIT,
        states=["done", "cancel"],
        order_by="desc",
        start_time=start_at,
        end_time=end_at,
    )
    if len(rows) >= _UPBIT_CLOSED_ORDERS_LIMIT:
        if end_at - start_at <= timedelta(hours=1):
            raise RuntimeError(
                "Upbit closed orders may be truncated in a <=1h window; "
                f"start={start_at.isoformat()} end={end_at.isoformat()}"
            )
        midpoint = start_at + (end_at - start_at) / 2
        left = await _fetch_upbit_closed_window(start_at, midpoint)
        right = await _fetch_upbit_closed_window(midpoint, end_at)
        return left + right
    return rows
```

Update `_fetch_upbit_filled` to use the explicit window and dedup:

```python
start_kst, end_kst = _resolve_kst_window(days=days, start_at=start_at, end_at=end_at)
all_fills: list[dict[str, Any]] = []
seen_order_uuids: set[str] = set()

for window_start, window_end in _iter_upbit_windows(start_kst, end_kst):
    closed = await _fetch_upbit_closed_window(window_start, window_end)
    for raw in closed:
        uuid = str(raw.get("uuid") or "")
        if uuid and uuid in seen_order_uuids:
            continue
        if uuid:
            seen_order_uuids.add(uuid)
        executed_vol = float(raw.get("executed_volume") or 0)
        if executed_vol <= 0:
            continue
        if not raw.get("trades"):
            try:
                raw = await upbit_service.fetch_order_detail(uuid)
            except Exception as exc:
                logger.warning(
                    "Upbit order detail fetch failed for %s: %s",
                    uuid,
                    exc,
                )
        for fill in normalize_upbit_order(raw):
            parsed_filled_at = _parse_upbit_fill_datetime(fill.get("filled_at", ""))
            if parsed_filled_at is None:
                logger.warning(
                    "Upbit filled order skipped due to invalid filled_at: "
                    "order_id=%s filled_at=%r",
                    fill.get("order_id"),
                    fill.get("filled_at"),
                )
                continue
            if start_kst <= parsed_filled_at <= end_kst:
                all_fills.append(fill)
```

No loop should break merely because a window contains only zero-fill cancels.

- [ ] **Step 8: Run Upbit tests**

Run:

```bash
uv run pytest \
  tests/test_upbit_orders.py \
  tests/test_n8n_trade_review.py::TestFilledOrdersService \
  -v
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add \
  app/services/brokers/upbit/orders.py \
  app/core/config.py \
  app/services/n8n_filled_orders_service.py \
  tests/test_upbit_orders.py \
  tests/test_n8n_trade_review.py
git commit -m "fix(ROB-480): crawl Upbit closed orders by time window"
```

---

### Task 5: Opening-Lot Seed Planning Service

**Files:**
- Create: `app/services/execution_ledger/opening_lots.py`
- Create: `tests/services/execution_ledger/test_opening_lots.py`
- Modify: `app/services/brokers/upbit/client.py`
- Modify: `tests/services/execution_ledger/test_no_broker_mutation.py`

- [ ] **Step 1: Write pure service tests**

```python
# tests/services/execution_ledger/test_opening_lots.py
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.services.execution_ledger.opening_lots import (
    OpeningLotCandidate,
    build_opening_lot_plan,
)


def _candidate(**overrides) -> OpeningLotCandidate:  # noqa: ANN003
    data = {
        "broker": "kis",
        "account_mode": "live",
        "venue": "krx",
        "instrument_type": "equity_kr",
        "symbol": "005930",
        "raw_symbol": "005930",
        "currency": "KRW",
        "current_qty": Decimal("10"),
        "avg_price": Decimal("70000"),
        "avg_price_modified": False,
    }
    data.update(overrides)
    return OpeningLotCandidate(**data)


def test_opening_lot_quantity_subtracts_ledger_net_since_cutover() -> None:
    cutover = datetime(2026, 5, 10, tzinfo=UTC)
    plan = build_opening_lot_plan(
        candidates=[_candidate()],
        ledger_net_by_key={("kis", "live", "krx", "equity_kr", "005930", "KRW"): Decimal("3")},
        cutover=cutover,
    )

    assert len(plan.upserts) == 1
    upsert = plan.upserts[0]
    assert upsert.source == "manual_import"
    assert upsert.side == "buy"
    assert upsert.filled_qty == Decimal("7")
    assert upsert.filled_price == Decimal("70000")
    assert upsert.filled_at == cutover
    assert upsert.broker_order_id == "SEED-20260510-kis-krx-005930"


def test_opening_lot_skips_when_ledger_net_covers_current_position() -> None:
    plan = build_opening_lot_plan(
        candidates=[_candidate(current_qty=Decimal("10"))],
        ledger_net_by_key={("kis", "live", "krx", "equity_kr", "005930", "KRW"): Decimal("10")},
        cutover=datetime(2026, 5, 10, tzinfo=UTC),
    )

    assert plan.upserts == []
    assert plan.skipped[0].reason == "covered_by_ledger_net"


def test_opening_lot_skips_modified_upbit_average_price() -> None:
    plan = build_opening_lot_plan(
        candidates=[
            _candidate(
                broker="upbit",
                venue="upbit_krw",
                instrument_type="crypto",
                symbol="SOL",
                raw_symbol="KRW-SOL",
                avg_price_modified=True,
            )
        ],
        ledger_net_by_key={},
        cutover=datetime(2026, 5, 10, tzinfo=UTC),
    )

    assert plan.upserts == []
    assert plan.skipped[0].reason == "upbit_avg_price_modified"


def test_opening_lot_skips_zero_average_price() -> None:
    plan = build_opening_lot_plan(
        candidates=[_candidate(avg_price=Decimal("0"))],
        ledger_net_by_key={},
        cutover=datetime(2026, 5, 10, tzinfo=UTC),
    )

    assert plan.upserts == []
    assert plan.skipped[0].reason == "non_positive_avg_price"
```

- [ ] **Step 2: Verify tests fail**

Run: `uv run pytest tests/services/execution_ledger/test_opening_lots.py -v`

Expected: FAIL because `opening_lots.py` does not exist.

- [ ] **Step 3: Implement opening lot service**

```python
# app/services/execution_ledger/opening_lots.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

from app.schemas.execution_ledger import (
    AccountMode,
    Broker,
    Currency,
    ExecutionLedgerUpsert,
    InstrumentTypeValue,
)


MatchKey = tuple[str, str, str, str, str, str]


@dataclass(frozen=True, slots=True)
class OpeningLotCandidate:
    broker: Broker
    account_mode: AccountMode
    venue: str
    instrument_type: InstrumentTypeValue
    symbol: str
    raw_symbol: str
    currency: Currency
    current_qty: Decimal
    avg_price: Decimal
    avg_price_modified: bool = False


@dataclass(frozen=True, slots=True)
class OpeningLotSkip:
    key: MatchKey
    reason: Literal[
        "covered_by_ledger_net",
        "non_positive_current_qty",
        "non_positive_avg_price",
        "upbit_avg_price_modified",
    ]
    current_qty: Decimal
    ledger_net_qty: Decimal


@dataclass(slots=True)
class OpeningLotPlan:
    upserts: list[ExecutionLedgerUpsert] = field(default_factory=list)
    skipped: list[OpeningLotSkip] = field(default_factory=list)


def _match_key(candidate: OpeningLotCandidate) -> MatchKey:
    return (
        candidate.broker,
        candidate.account_mode,
        candidate.venue,
        candidate.instrument_type,
        candidate.symbol,
        candidate.currency,
    )


def _seed_order_id(candidate: OpeningLotCandidate, cutover: datetime) -> str:
    return (
        f"SEED-{cutover:%Y%m%d}-"
        f"{candidate.broker}-{candidate.venue}-{candidate.symbol}"
    )


def build_opening_lot_plan(
    *,
    candidates: list[OpeningLotCandidate],
    ledger_net_by_key: dict[MatchKey, Decimal],
    cutover: datetime,
) -> OpeningLotPlan:
    plan = OpeningLotPlan()
    for candidate in candidates:
        key = _match_key(candidate)
        ledger_net_qty = ledger_net_by_key.get(key, Decimal("0"))
        if candidate.current_qty <= 0:
            plan.skipped.append(
                OpeningLotSkip(key, "non_positive_current_qty", candidate.current_qty, ledger_net_qty)
            )
            continue
        if candidate.avg_price <= 0:
            plan.skipped.append(
                OpeningLotSkip(key, "non_positive_avg_price", candidate.current_qty, ledger_net_qty)
            )
            continue
        if candidate.broker == "upbit" and candidate.avg_price_modified:
            plan.skipped.append(
                OpeningLotSkip(key, "upbit_avg_price_modified", candidate.current_qty, ledger_net_qty)
            )
            continue

        opening_qty = candidate.current_qty - ledger_net_qty
        if opening_qty <= 0:
            plan.skipped.append(
                OpeningLotSkip(key, "covered_by_ledger_net", candidate.current_qty, ledger_net_qty)
            )
            continue

        plan.upserts.append(
            ExecutionLedgerUpsert(
                broker=candidate.broker,
                account_mode=candidate.account_mode,
                venue=candidate.venue,
                instrument_type=candidate.instrument_type,
                symbol=candidate.symbol,
                raw_symbol=candidate.raw_symbol,
                side="buy",
                broker_order_id=_seed_order_id(candidate, cutover),
                fill_seq=0,
                filled_qty=opening_qty,
                filled_price=candidate.avg_price,
                filled_at=cutover,
                currency=candidate.currency,
                source="manual_import",
                raw_payload_json={
                    "seed_kind": "opening_lot",
                    "current_qty": str(candidate.current_qty),
                    "ledger_net_qty": str(ledger_net_qty),
                    "cutover": cutover.isoformat(),
                },
            )
        )
    return plan
```

- [ ] **Step 4: Preserve Upbit modified-average flag**

Change `parse_upbit_account_row` in `app/services/brokers/upbit/client.py`:

```python
def parse_upbit_account_row(account: dict[str, Any]) -> dict[str, float | bool]:
    balance = float(account.get("balance", 0) or 0)
    locked = float(account.get("locked", 0) or 0)
    avg_buy_price = float(account.get("avg_buy_price", 0) or 0)
    avg_buy_price_modified = str(
        account.get("avg_buy_price_modified", "false")
    ).lower() in {"true", "1", "yes"}
    return {
        "balance": balance,
        "locked": locked,
        "total_quantity": balance + locked,
        "orderable_quantity": balance,
        "avg_buy_price": avg_buy_price,
        "avg_buy_price_modified": avg_buy_price_modified,
    }
```

Add a unit assertion in `tests/test_services_upbit.py` or `tests/test_upbit_service.py`, whichever already imports `parse_upbit_account_row`:

```python
def test_parse_upbit_account_row_preserves_modified_average_flag():
    from app.services.brokers.upbit.client import parse_upbit_account_row

    parsed = parse_upbit_account_row(
        {
            "currency": "SOL",
            "balance": "1.5",
            "locked": "0.5",
            "avg_buy_price": "100000",
            "avg_buy_price_modified": True,
        }
    )

    assert parsed["total_quantity"] == 2.0
    assert parsed["avg_buy_price"] == 100000.0
    assert parsed["avg_buy_price_modified"] is True
```

- [ ] **Step 5: Run service tests and guard**

Run:

```bash
uv run pytest \
  tests/services/execution_ledger/test_opening_lots.py \
  tests/services/execution_ledger/test_no_broker_mutation.py \
  tests/test_services_upbit.py \
  -v
```

Expected: PASS. If the Upbit parser test belongs in `tests/test_upbit_service.py` after inspection, run that file instead of `tests/test_services_upbit.py`.

- [ ] **Step 6: Commit**

```bash
git add \
  app/services/execution_ledger/opening_lots.py \
  app/services/brokers/upbit/client.py \
  tests/services/execution_ledger/test_opening_lots.py \
  tests/services/execution_ledger/test_no_broker_mutation.py \
  tests/test_services_upbit.py \
  tests/test_upbit_service.py
git commit -m "feat(ROB-481): plan manual opening lots from broker averages"
```

---

### Task 6: Manual Import Seed CLI

**Files:**
- Create: `scripts/seed_execution_ledger_opening_lots.py`
- Create: `tests/scripts/test_seed_execution_ledger_opening_lots_cli.py`
- Modify: `app/services/execution_ledger/opening_lots.py`
- Modify: `app/services/execution_ledger/repository.py`

- [ ] **Step 1: Add CLI tests**

```python
# tests/scripts/test_seed_execution_ledger_opening_lots_cli.py
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import scripts.seed_execution_ledger_opening_lots as cli
from app.services.execution_ledger.opening_lots import OpeningLotCandidate


def _candidate() -> OpeningLotCandidate:
    return OpeningLotCandidate(
        broker="kis",
        account_mode="live",
        venue="krx",
        instrument_type="equity_kr",
        symbol="005930",
        raw_symbol="005930",
        currency="KRW",
        current_qty=Decimal("10"),
        avg_price=Decimal("70000"),
    )


@pytest.mark.asyncio
async def test_seed_cli_dry_run_rolls_back(monkeypatch):
    session = AsyncMock()
    session.rollback = AsyncMock()
    session.commit = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(cli, "load_opening_lot_candidates", AsyncMock(return_value=[_candidate()]))
    monkeypatch.setattr(
        cli,
        "load_ledger_net_by_key_since",
        AsyncMock(return_value={}),
    )

    rc = await cli._run(
        brokers=["kis"],
        cutover=datetime(2026, 5, 10, tzinfo=UTC),
        dry_run=True,
    )

    assert rc == 0
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_cli_commit_requires_gate(monkeypatch):
    monkeypatch.setattr(
        cli,
        "settings",
        SimpleNamespace(EXECUTION_LEDGER_COMMIT_ENABLED=False),
    )

    with pytest.raises(RuntimeError, match="EXECUTION_LEDGER_COMMIT_ENABLED"):
        await cli._run(
            brokers=["kis"],
            cutover=datetime(2026, 5, 10, tzinfo=UTC),
            dry_run=False,
        )
```

- [ ] **Step 2: Verify tests fail**

Run: `uv run pytest tests/scripts/test_seed_execution_ledger_opening_lots_cli.py -v`

Expected: FAIL because the CLI does not exist.

- [ ] **Step 3: Add ledger net loader**

In `app/services/execution_ledger/repository.py`, add a read-only helper:

```python
from sqlalchemy import case


async def net_quantity_by_match_key_since(
    self, *, cutover: datetime
) -> dict[tuple[str, str, str, str, str, str], Decimal]:
    signed_qty = case(
        (ExecutionLedger.side == "buy", ExecutionLedger.filled_qty),
        else_=-ExecutionLedger.filled_qty,
    )
    rows = await self.db.execute(
        select(
            ExecutionLedger.broker,
            ExecutionLedger.account_mode,
            ExecutionLedger.venue,
            ExecutionLedger.instrument_type,
            ExecutionLedger.symbol,
            ExecutionLedger.currency,
            func.coalesce(func.sum(signed_qty), 0),
        )
        .where(ExecutionLedger.filled_at >= cutover)
        .where(ExecutionLedger.source != "manual_import")
        .group_by(
            ExecutionLedger.broker,
            ExecutionLedger.account_mode,
            ExecutionLedger.venue,
            ExecutionLedger.instrument_type,
            ExecutionLedger.symbol,
            ExecutionLedger.currency,
        )
    )
    return {
        (broker, account_mode, venue, str(instrument_type), symbol, currency): Decimal(str(net_qty))
        for broker, account_mode, venue, instrument_type, symbol, currency, net_qty in rows.all()
    }
```

- [ ] **Step 4: Add candidate loaders**

In `app/services/execution_ledger/opening_lots.py`, add read-only loaders:

```python
async def load_opening_lot_candidates(
    brokers: list[str],
) -> list[OpeningLotCandidate]:
    candidates: list[OpeningLotCandidate] = []
    if "kis" in brokers:
        candidates.extend(await load_kis_opening_lot_candidates())
    if "upbit" in brokers:
        candidates.extend(await load_upbit_opening_lot_candidates())
    return candidates
```

KIS loader:

```python
async def load_kis_opening_lot_candidates() -> list[OpeningLotCandidate]:
    kis = KISClient()
    candidates: list[OpeningLotCandidate] = []
    for row in await kis.fetch_my_stocks():
        qty = Decimal(str(row.get("hldg_qty") or "0"))
        avg_price = Decimal(str(row.get("pchs_avg_pric") or "0"))
        symbol = str(row.get("pdno") or "").strip().upper()
        if symbol:
            candidates.append(
                OpeningLotCandidate(
                    broker="kis",
                    account_mode="live",
                    venue="krx",
                    instrument_type="equity_kr",
                    symbol=symbol,
                    raw_symbol=symbol,
                    currency="KRW",
                    current_qty=qty,
                    avg_price=avg_price,
                )
            )
    for row in await kis.fetch_my_us_stocks():
        symbol = str(row.get("ovrs_pdno") or "").strip().upper()
        venue = str(row.get("ovrs_excg_cd") or row.get("excg_cd") or "").strip().upper()
        if not venue:
            venue = "NASD"
        qty = Decimal(str(row.get("ovrs_cblc_qty") or "0"))
        avg_price = Decimal(str(row.get("pchs_avg_pric") or "0"))
        if symbol:
            candidates.append(
                OpeningLotCandidate(
                    broker="kis",
                    account_mode="live",
                    venue=venue,
                    instrument_type="equity_us",
                    symbol=symbol,
                    raw_symbol=symbol,
                    currency="USD",
                    current_qty=qty,
                    avg_price=avg_price,
                )
            )
    return candidates
```

Upbit loader:

```python
async def load_upbit_opening_lot_candidates() -> list[OpeningLotCandidate]:
    rows = await fetch_my_coins()
    candidates: list[OpeningLotCandidate] = []
    for row in rows:
        currency = str(row.get("currency") or "").strip().upper()
        if not currency or currency == "KRW":
            continue
        unit_currency = str(row.get("unit_currency") or "KRW").strip().upper()
        parsed = parse_upbit_account_row(row)
        current_qty = Decimal(str(parsed["total_quantity"]))
        avg_price = Decimal(str(parsed["avg_buy_price"]))
        candidates.append(
            OpeningLotCandidate(
                broker="upbit",
                account_mode="live",
                venue=f"upbit_{unit_currency.lower()}",
                instrument_type="crypto",
                symbol=currency,
                raw_symbol=f"{unit_currency}-{currency}",
                currency="KRW" if unit_currency == "KRW" else "USD",
                current_qty=current_qty,
                avg_price=avg_price,
                avg_price_modified=bool(parsed["avg_buy_price_modified"]),
            )
        )
    return candidates
```

- [ ] **Step 5: Implement seed CLI**

```python
# scripts/seed_execution_ledger_opening_lots.py
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.execution_ledger.opening_lots import (
    build_opening_lot_plan,
    load_opening_lot_candidates,
)
from app.services.execution_ledger.repository import ExecutionLedgerRepository


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed manual_import opening lots into execution_ledger."
    )
    parser.add_argument("--broker", choices=["kis", "upbit"], action="append")
    parser.add_argument("--cutover", required=True, help="UTC cutover date YYYY-MM-DD")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--commit", action="store_true")
    return parser.parse_args()


def parse_cutover(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)


async def _run(*, brokers: list[str], cutover: datetime, dry_run: bool) -> int:
    if not dry_run and not settings.EXECUTION_LEDGER_COMMIT_ENABLED:
        raise RuntimeError("EXECUTION_LEDGER_COMMIT_ENABLED is false; commit mode is disabled")
    async with AsyncSessionLocal() as db:
        repo = ExecutionLedgerRepository(db)
        candidates = await load_opening_lot_candidates(brokers)
        ledger_net = await repo.net_quantity_by_match_key_since(cutover=cutover)
        plan = build_opening_lot_plan(
            candidates=candidates,
            ledger_net_by_key=ledger_net,
            cutover=cutover,
        )
        committed = 0
        for upsert in plan.upserts:
            status = await repo.classify_fill(upsert)
            if not dry_run and status != "unchanged":
                await repo.upsert_fill(upsert)
                committed += 1
        if dry_run:
            await db.rollback()
        else:
            await db.commit()
        print(
            json.dumps(
                {
                    "dry_run": dry_run,
                    "would_seed": len(plan.upserts),
                    "committed": committed,
                    "skipped": [asdict(skip) for skip in plan.skipped],
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        )
    return 0


def main() -> int:
    args = parse_args()
    brokers = args.broker or ["kis", "upbit"]
    return asyncio.run(
        _run(
            brokers=brokers,
            cutover=parse_cutover(args.cutover),
            dry_run=not bool(args.commit),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Run seed tests**

Run:

```bash
uv run pytest \
  tests/services/execution_ledger/test_opening_lots.py \
  tests/scripts/test_seed_execution_ledger_opening_lots_cli.py \
  tests/services/execution_ledger/test_repository.py \
  -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add \
  app/services/execution_ledger/opening_lots.py \
  app/services/execution_ledger/repository.py \
  scripts/seed_execution_ledger_opening_lots.py \
  tests/services/execution_ledger/test_opening_lots.py \
  tests/scripts/test_seed_execution_ledger_opening_lots_cli.py
git commit -m "feat(ROB-481): add dry-run opening lot seed cli"
```

---

### Task 7: End-To-End Verification And Operator Evidence

**Files:**
- Modify: `docs/runbooks/execution-ledger-backfill.md` if it exists.
- Create: `docs/runbooks/execution-ledger-backfill.md` if it does not exist.

- [ ] **Step 1: Run focused tests**

```bash
uv run pytest \
  tests/services/execution_ledger/test_query_service_profit.py \
  tests/services/execution_ledger/test_reconciler.py \
  tests/services/execution_ledger/test_normalizers.py \
  tests/services/execution_ledger/test_repository.py \
  tests/services/execution_ledger/test_opening_lots.py \
  tests/test_kis_domestic_orders_retry.py \
  tests/test_kis_overseas_orders_retry.py \
  tests/test_upbit_orders.py \
  tests/test_n8n_trade_review.py::TestFilledOrdersService \
  tests/scripts/test_reconcile_execution_ledger_cli.py \
  tests/scripts/test_seed_execution_ledger_opening_lots_cli.py \
  -v
```

Expected: PASS.

- [ ] **Step 2: Run static checks**

```bash
uv run ruff check app/ tests/ scripts/reconcile_execution_ledger.py scripts/seed_execution_ledger_opening_lots.py
uv run ruff format --check app/ tests/ scripts/reconcile_execution_ledger.py scripts/seed_execution_ledger_opening_lots.py
uv run ty check app/ --error-on-warning
```

Expected: all PASS.

- [ ] **Step 3: Run full non-live test gate**

```bash
make test
```

Expected: PASS.

- [ ] **Step 4: Add operator runbook**

`docs/runbooks/execution-ledger-backfill.md` must include these exact phases:

````markdown
# Execution Ledger Backfill Runbook

## Review Hold

Do not run commit mode until ROB-478~481 are reviewed under `high_risk_change` and `needs_stronger_model_review`.

## Phase 1: KIS Dry Run

```bash
uv run python -m scripts.reconcile_execution_ledger \
  --broker kis \
  --start-date 2026-02-01 \
  --end-date 2026-06-10 \
  --max-pages 100 \
  --dry-run
```

Archive JSON output with `would_insert`, `would_update`, `unchanged`, and sample rows.

## Phase 2: Upbit Dry Run

```bash
uv run python -m scripts.reconcile_execution_ledger \
  --broker upbit \
  --start-date 2026-02-01 \
  --end-date 2026-06-10 \
  --dry-run
```

Archive JSON output and confirm no truncation error.

## Phase 3: Coverage SQL

Run the ROB-478 coverable SQL before and after dry-run planning against the target DB.

## Phase 4: KIS/Upbit Commit

Only after reviewer approval:

```bash
EXECUTION_LEDGER_COMMIT_ENABLED=true uv run python -m scripts.reconcile_execution_ledger \
  --broker kis \
  --start-date 2026-02-01 \
  --end-date 2026-06-10 \
  --max-pages 100 \
  --commit

EXECUTION_LEDGER_COMMIT_ENABLED=true uv run python -m scripts.reconcile_execution_ledger \
  --broker upbit \
  --start-date 2026-02-01 \
  --end-date 2026-06-10 \
  --commit
```

## Phase 5: Opening Lot Seed Dry Run

Run only after Phase 4 commits:

```bash
uv run python -m scripts.seed_execution_ledger_opening_lots \
  --cutover 2026-05-10 \
  --dry-run
```

Archive skipped rows. Modified Upbit average prices and ambiguous/non-positive prices must remain skipped.

## Phase 6: Opening Lot Seed Commit

Only after reviewer approval:

```bash
EXECUTION_LEDGER_COMMIT_ENABLED=true uv run python -m scripts.seed_execution_ledger_opening_lots \
  --cutover 2026-05-10 \
  --commit
```

## Phase 7: UI Verification

Open `/invest/my?tab=sellHistory` and confirm matched rows show 판매수익/수익률 and currency summary cards render.
````

- [ ] **Step 5: Commit runbook**

```bash
git add docs/runbooks/execution-ledger-backfill.md
git commit -m "docs(ROB-478): add execution ledger backfill runbook"
```

- [ ] **Step 6: Final Linear comments**

After tests pass and before merge, comment on `ROB-478`:

```markdown
Implementation is ready for ROB-478~481, but this remains under `needs_stronger_model_review`. No production commit backfill or `manual_import` seed execution until reviewer clears:

- focused tests
- `make test`
- KIS dry-run JSON
- Upbit dry-run JSON
- pre/post coverable SQL
- opening-lot seed dry-run JSON
```

---

## Self-Review

**Spec coverage:** `ROB-479` is covered by Tasks 1-3; `ROB-480` is covered by Task 4; `ROB-481` is covered by Tasks 5-6; parent `ROB-478` acceptance and operator order are covered by Task 7.

**Placeholder scan:** The plan avoids deferred implementation language and provides concrete file paths, code snippets, commands, expected outcomes, and commit messages.

**Type consistency:** The plan uses existing schema names: `ExecutionLedgerUpsert`, `ExecutionLedgerRead`, `ExecutionLedgerRepository`, `ExecutionLedgerReconciler`, `source='manual_import'`, and the FIFO 6-tuple `(broker, account_mode, venue, instrument_type, symbol, currency)`.

**Execution order:** Code for PR1/PR2/PR3 can be implemented in parallel after Task 0, but operator execution must be: KIS dry-run/commit, Upbit dry-run/commit, coverage SQL, opening-lot seed dry-run, opening-lot seed commit.

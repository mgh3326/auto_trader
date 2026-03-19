# N8N KR Morning Report Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add `GET /api/n8n/kr-morning-report` that returns a single pre-market KR-only report for n8n, combining KR holdings, KIS orderable KRW cash, KR screener output, and KR pending orders with Discord-ready `brief_text`.

**Architecture:** Add a dedicated service `app/services/n8n_kr_morning_report_service.py` that orchestrates existing `PortfolioOverviewService`, `screen_stocks_impl`, `fetch_pending_orders`, and `KISClient.inquire_domestic_cash_balance()` in parallel. Keep the HTTP route thin in `app/routers/n8n.py`, add typed response models in `app/schemas/n8n.py`, and cover behavior with focused service tests plus endpoint tests. `toss` cash is explicitly out of scope for automation in this iteration: expose `toss_krw=None` and `toss_krw_fmt="수동 관리"`, and compute `total_krw` from KIS only.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy async session, existing KIS client, existing MCP screener handler, pytest, Ruff

---

## Key Code References

| File | Role |
|------|------|
| `app/routers/n8n.py` | Existing n8n endpoint style and exception handling |
| `app/services/n8n_daily_brief_service.py` | Parallel orchestration and `brief_text` builder pattern |
| `app/services/n8n_pending_orders_service.py` | Pending order normalization and formatting reuse |
| `app/services/n8n_formatting.py` | `fmt_amount`, `fmt_price`, `fmt_pnl`, `fmt_date_with_weekday` |
| `app/services/portfolio_overview_service.py` | Combined holdings source including KIS and manual/Toss positions |
| `app/mcp_server/tooling/analysis_tool_handlers.py` | `screen_stocks_impl()` contract and strategy validation |
| `app/services/brokers/kis/client.py` | `KISClient.inquire_domestic_cash_balance()` |
| `docs/n8n-api-reference.md` | n8n API documentation index |

## Design Decisions

1. Add this endpoint to the existing `app/routers/n8n.py` instead of creating a new router file. The route belongs to the established `/api/n8n` family and follows the same service-backed pattern as `daily-brief`, `pending-orders`, and `crypto-scan`.
2. Keep all composition logic in a new dedicated service instead of bloating `n8n.py` or reusing `n8n_daily_brief_service.py`. The KR morning payload has materially different grouping and formatting concerns.
3. Treat `toss` cash as an explicit manual-only field in this iteration. The response stays structurally stable for n8n, but the numeric value is `null` and the display string is `"수동 관리"`.
4. Sort screening results by RSI ascending after service normalization, regardless of the upstream screener’s default sort, so the morning report consistently prioritizes oversold candidates.
5. Reuse the existing `/api/n8n/*` auth behavior already wired by `AuthMiddleware`; no middleware or `app/main.py` changes are needed.

## Response Contract Notes

- `cash_balance.kis_krw`: numeric KIS orderable KRW balance.
- `cash_balance.toss_krw`: `null`.
- `cash_balance.toss_krw_fmt`: `"수동 관리"`.
- `cash_balance.total_krw`: same numeric value as `kis_krw`.
- `cash_balance.total_krw_fmt`: same formatted value as `kis_krw_fmt`.
- `holdings.toss`: populated from manual holdings whose `broker` resolves to `toss`.
- `holdings.combined`: KR holdings only, combining KIS and Toss/manual KR positions.
- `screening.results`: normalized top `N` rows only.
- `pending_orders`: reuse `fetch_pending_orders(market="kr")` output shape as much as possible, but strip to the morning-report summary object.

---

### Task 1: Add the KR Morning Report schemas

**Files:**
- Modify: `app/schemas/n8n.py`
- Check: `app/routers/n8n.py`

**Step 1: Write the failing schema test**

Add to a new test file `tests/test_n8n_kr_morning_report.py`:

```python
from app.schemas.n8n import N8nKrMorningReportResponse


def test_kr_morning_report_schema_accepts_manual_toss_cash():
    payload = N8nKrMorningReportResponse(
        success=True,
        as_of="2026-03-19T08:50:00+09:00",
        date_fmt="03/19 (목)",
        cash_balance={
            "kis_krw": 45000,
            "kis_krw_fmt": "4.5만",
            "toss_krw": None,
            "toss_krw_fmt": "수동 관리",
            "total_krw": 45000,
            "total_krw_fmt": "4.5만",
        },
    )

    assert payload.cash_balance.toss_krw is None
    assert payload.cash_balance.toss_krw_fmt == "수동 관리"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_n8n_kr_morning_report.py::test_kr_morning_report_schema_accepts_manual_toss_cash -v --no-header`

Expected: FAIL with `ImportError` or missing schema classes.

**Step 3: Add the schemas**

Update `app/schemas/n8n.py` with:

- `N8nKrPosition`
- `N8nKrHoldingsAccount`
- `N8nKrHoldings`
- `N8nKrCashBalance`
- `N8nKrScreenResult`
- `N8nKrScreening`
- `N8nKrPendingOrdersSummary` or keep `pending_orders: dict[str, Any]` if you want to minimize schema surface
- `N8nKrMorningReportResponse`

Use these concrete field choices:

```python
class N8nKrCashBalance(BaseModel):
    kis_krw: float = 0
    kis_krw_fmt: str = "0"
    toss_krw: float | None = None
    toss_krw_fmt: str = "수동 관리"
    total_krw: float = 0
    total_krw_fmt: str = "0"
```

Also use `Field(default_factory=...)` for list/dict defaults instead of mutable literals.

**Step 4: Run the schema test**

Run: `uv run pytest tests/test_n8n_kr_morning_report.py::test_kr_morning_report_schema_accepts_manual_toss_cash -v --no-header`

Expected: PASS.

**Step 5: Commit**

```bash
git add app/schemas/n8n.py tests/test_n8n_kr_morning_report.py
git commit -m "feat(n8n): add KR morning report schemas"
```

---

### Task 2: Add service tests for holdings grouping and cash policy

**Files:**
- Create: `tests/test_n8n_kr_morning_report.py`
- Check: `app/services/portfolio_overview_service.py`

**Step 1: Write the failing test for KR holdings grouping**

Add:

```python
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from app.core.timezone import KST


@pytest.mark.asyncio
async def test_fetch_kr_morning_report_groups_kis_and_toss_kr_holdings():
    as_of = datetime(2026, 3, 19, 8, 50, tzinfo=KST)
    overview = {
        "positions": [
            {
                "market_type": "KR",
                "symbol": "005930",
                "name": "삼성전자",
                "quantity": 10,
                "avg_price": 70000,
                "current_price": 68000,
                "evaluation": 680000,
                "profit_rate": -0.0286,
                "broker": "kis",
            },
            {
                "market_type": "KR",
                "symbol": "000660",
                "name": "SK하이닉스",
                "quantity": 3,
                "avg_price": 200000,
                "current_price": 210000,
                "evaluation": 630000,
                "profit_rate": 0.05,
                "broker": "toss",
            },
            {
                "market_type": "US",
                "symbol": "AAPL",
                "name": "Apple",
                "quantity": 1,
                "avg_price": 200,
                "current_price": 205,
                "evaluation": 205,
                "profit_rate": 0.025,
                "broker": "kis",
            },
        ]
    }

    with (
        patch(
            "app.services.n8n_kr_morning_report_service._get_portfolio_overview",
            new_callable=AsyncMock,
            return_value=overview,
        ),
        patch(
            "app.services.n8n_kr_morning_report_service._fetch_kis_cash_balance",
            new_callable=AsyncMock,
            return_value=45000.0,
        ),
        patch(
            "app.services.n8n_kr_morning_report_service.fetch_pending_orders",
            new_callable=AsyncMock,
            return_value={"total": 0, "buy_count": 0, "sell_count": 0, "orders": []},
        ),
        patch(
            "app.services.n8n_kr_morning_report_service._fetch_screening",
            new_callable=AsyncMock,
            return_value={"total_scanned": 0, "top_n": 20, "strategy": None, "results": [], "summary": {}},
        ),
    ):
        from app.services.n8n_kr_morning_report_service import fetch_kr_morning_report

        result = await fetch_kr_morning_report(as_of=as_of)

    assert result["holdings"]["kis"]["total_count"] == 1
    assert result["holdings"]["toss"]["total_count"] == 1
    assert result["holdings"]["combined"]["total_count"] == 2
    assert result["cash_balance"]["kis_krw"] == 45000.0
    assert result["cash_balance"]["toss_krw"] is None
    assert result["cash_balance"]["toss_krw_fmt"] == "수동 관리"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_n8n_kr_morning_report.py::test_fetch_kr_morning_report_groups_kis_and_toss_kr_holdings -v --no-header`

Expected: FAIL because the service module does not exist.

**Step 3: Add one more failing test for no-KR holdings**

Add:

```python
@pytest.mark.asyncio
async def test_fetch_kr_morning_report_returns_zeroed_holdings_when_no_kr_positions():
    ...
    assert result["holdings"]["combined"]["total_count"] == 0
    assert result["holdings"]["combined"]["total_eval_fmt"] == "0"
```

Mock overview with only `US`/`CRYPTO` positions.

**Step 4: Run both tests**

Run: `uv run pytest tests/test_n8n_kr_morning_report.py -k "groups_kis_and_toss or no_kr_positions" -v --no-header`

Expected: FAIL.

**Step 5: Commit**

```bash
git add tests/test_n8n_kr_morning_report.py
git commit -m "test(n8n): add KR morning holdings service tests"
```

---

### Task 3: Implement the KR morning report service

**Files:**
- Create: `app/services/n8n_kr_morning_report_service.py`
- Check: `app/services/n8n_daily_brief_service.py`
- Check: `app/services/n8n_pending_orders_service.py`
- Check: `app/services/n8n_formatting.py`

**Step 1: Create minimal service helpers**

Create `app/services/n8n_kr_morning_report_service.py` with:

- `fetch_kr_morning_report(...)`
- `_get_portfolio_overview()`
- `_fetch_kis_cash_balance()`
- `_fetch_screening(...)`
- `_build_holdings(...)`
- `_build_pending_summary(...)`
- `_build_brief_text(...)`

Use this service signature:

```python
async def fetch_kr_morning_report(
    include_screen: bool = True,
    screen_strategy: str | None = None,
    include_pending: bool = True,
    top_n: int = 20,
    as_of: datetime | None = None,
) -> dict[str, Any]:
```

**Step 2: Implement parallel orchestration**

Inside `fetch_kr_morning_report()`:

```python
as_of_dt = (as_of or now_kst()).replace(microsecond=0)

portfolio_task = asyncio.create_task(_get_portfolio_overview())
cash_task = asyncio.create_task(_fetch_kis_cash_balance())
screen_task = (
    asyncio.create_task(_fetch_screening(screen_strategy=screen_strategy, top_n=top_n))
    if include_screen
    else None
)
pending_task = (
    asyncio.create_task(fetch_pending_orders(market="kr", include_current_price=True))
    if include_pending
    else None
)
```

Collect partial failures into `errors` instead of failing the entire response.

**Step 3: Implement holdings grouping**

Use `PortfolioOverviewService.get_overview(user_id=1)` and then:

- keep only `market_type == "KR"`
- route positions to:
  - `kis` when `broker == "kis"` or `account_key == "live:kis"`
  - `toss` when `broker == "toss"`
- ignore other KR manual brokers for now, or bucket them into `toss` only if product requirements clearly say Toss/manual are equivalent. Preferred: only `broker == "toss"` goes to `toss`, everything else stays out of this response until explicitly requested.

For each normalized position:

```python
{
    "symbol": pos["symbol"],
    "name": pos["name"],
    "quantity": float(pos.get("quantity") or 0),
    "avg_price": float(pos.get("avg_price") or 0),
    "current_price": float(pos.get("current_price") or 0) or None,
    "eval_krw": float(pos.get("evaluation") or 0) or None,
    "pnl_pct": round(float(pos["profit_rate"]) * 100, 1) if pos.get("profit_rate") is not None else None,
    "pnl_fmt": fmt_pnl(...),
    "eval_fmt": fmt_amount(float(pos.get("evaluation") or 0)),
    "account": account_name,
}
```

Account summary should compute:

- `total_count`
- `total_eval_krw`
- `total_eval_fmt`
- weighted `total_pnl_pct` from evaluation/cost or by reusing the `n8n_daily_brief_service._build_portfolio_summary` math if you extract a KR-specific helper instead of duplicating the formula

**Step 4: Implement KIS cash balance**

Use:

```python
client = KISClient()
payload = await client.inquire_domestic_cash_balance()
orderable = float(payload.get("stck_cash_ord_psbl_amt") or 0)
```

Return:

```python
{
    "kis_krw": orderable,
    "kis_krw_fmt": fmt_amount(orderable),
    "toss_krw": None,
    "toss_krw_fmt": "수동 관리",
    "total_krw": orderable,
    "total_krw_fmt": fmt_amount(orderable),
}
```

**Step 5: Implement screening normalization**

Create `_fetch_screening()` around `screen_stocks_impl()`:

```python
raw = await screen_stocks_impl(
    market="kr",
    strategy=screen_strategy,
    limit=max(top_n, 30),
)
```

Then:

- extract rows from `raw["results"]`
- sort with `key=lambda row: (row.get("rsi") is None, float(row.get("rsi") or 999))`
- slice `[:top_n]`
- normalize fields to `rsi_14`, `change_pct`, `volume_ratio`, `market_cap_fmt`, `signal`, `sector`
- compute summary:
  - `oversold_count = len([r for r in rows if rsi < 30])`
  - `overbought_count = len([r for r in rows if rsi > 70])`
  - `avg_rsi`
  - `top_signal`

If the upstream row uses different key names, normalize them explicitly and document the mapping in comments.

**Step 6: Implement pending orders summary**

If `include_pending` is true:

- call `fetch_pending_orders(market="kr", include_current_price=True, side=None, min_amount=0)`
- collapse into:

```python
{
    "total": ...,
    "buy_count": ...,
    "sell_count": ...,
    "total_buy_fmt": ...,
    "total_sell_fmt": ...,
    "orders": orders[:10],
}
```

Reuse `summary_line` from the pending order service; do not rebuild it here unless missing.

**Step 7: Implement the Discord `brief_text` builder**

Build a dedicated `_build_brief_text()` for this endpoint.

Required sections:

```text
📊 KR 모닝 리포트 — {date_fmt}

💼 국내주식 잔고
KIS: {kis_eval_fmt} ({kis_pnl_fmt}) — {kis_count}종목
토스: {toss_eval_fmt} ({toss_pnl_fmt}) — {toss_count}종목
합산: {combined_eval_fmt} ({combined_pnl_fmt})

💰 가용 현금
KIS: {kis_cash_fmt} | 토스: 수동 관리 | 합산: {total_cash_fmt}
```

For pending/screening sections:

- include the section only when that feature is enabled
- cap detailed lines to 5 items in `brief_text`
- end with `상세 분석은 스레드에서 진행합니다.`

**Step 8: Make the service tests pass**

Run: `uv run pytest tests/test_n8n_kr_morning_report.py -k "groups_kis_and_toss or no_kr_positions" -v --no-header`

Expected: PASS.

**Step 9: Commit**

```bash
git add app/services/n8n_kr_morning_report_service.py tests/test_n8n_kr_morning_report.py
git commit -m "feat(n8n): add KR morning report service"
```

---

### Task 4: Add service tests for screening, pending toggles, and `brief_text`

**Files:**
- Modify: `tests/test_n8n_kr_morning_report.py`

**Step 1: Write a failing test for `include_screen=False`**

Add:

```python
@pytest.mark.asyncio
async def test_fetch_kr_morning_report_skips_screening_when_disabled():
    with (
        patch(..._get_portfolio_overview..., new_callable=AsyncMock, return_value={"positions": []}),
        patch(..._fetch_kis_cash_balance..., new_callable=AsyncMock, return_value=45000.0),
        patch(...fetch_pending_orders..., new_callable=AsyncMock, return_value={"total": 0, "buy_count": 0, "sell_count": 0, "orders": []}),
        patch(...screen_stocks_impl..., new_callable=AsyncMock) as screen_mock,
    ):
        result = await fetch_kr_morning_report(include_screen=False)

    screen_mock.assert_not_called()
    assert result["screening"]["results"] == []
```

**Step 2: Write a failing test for `top_n` trimming and RSI ordering**

Add:

```python
@pytest.mark.asyncio
async def test_fetch_kr_morning_report_sorts_screening_by_lowest_rsi_and_trims_top_n():
    raw_results = [
        {"symbol": "A", "name": "A", "current_price": 1000, "rsi": 40},
        {"symbol": "B", "name": "B", "current_price": 1000, "rsi": 22},
        {"symbol": "C", "name": "C", "current_price": 1000, "rsi": 31},
    ]
    ...
    result = await fetch_kr_morning_report(top_n=2)
    assert [row["symbol"] for row in result["screening"]["results"]] == ["B", "C"]
```

**Step 3: Write a failing test for `brief_text` formatting**

Add:

```python
def test_build_brief_text_formats_manual_toss_cash_label():
    from app.services.n8n_kr_morning_report_service import _build_brief_text

    text = _build_brief_text(
        date_fmt="03/19 (목)",
        holdings={...},
        cash_balance={
            "kis_krw_fmt": "4.5만",
            "toss_krw_fmt": "수동 관리",
            "total_krw_fmt": "4.5만",
        },
        screening={...},
        pending_orders={...},
        include_screen=True,
        include_pending=True,
    )

    assert "토스: 수동 관리" in text
    assert text.startswith("📊 KR 모닝 리포트 — 03/19 (목)")
```

**Step 4: Run the new tests**

Run: `uv run pytest tests/test_n8n_kr_morning_report.py -k "skips_screening or trims_top_n or brief_text_formats" -v --no-header`

Expected: FAIL.

**Step 5: Implement the missing behavior**

Adjust the service module until the tests pass:

- short-circuit screen fetch when `include_screen=False`
- normalize upstream screener key names
- trim and sort results deterministically
- make `brief_text` include the exact Toss cash wording

**Step 6: Run the targeted service test file**

Run: `uv run pytest tests/test_n8n_kr_morning_report.py -v --no-header`

Expected: PASS.

**Step 7: Commit**

```bash
git add app/services/n8n_kr_morning_report_service.py tests/test_n8n_kr_morning_report.py
git commit -m "test(n8n): cover KR morning report formatting and screening"
```

---

### Task 5: Add the API endpoint in the existing n8n router

**Files:**
- Modify: `app/routers/n8n.py`
- Check: `app/main.py`

**Step 1: Write the failing API test**

Add to `tests/test_n8n_api.py`:

```python
def test_get_kr_morning_report_success(client):
    payload = {
        "success": True,
        "as_of": "2026-03-19T08:50:00+09:00",
        "date_fmt": "03/19 (목)",
        "holdings": {"kis": {}, "toss": {}, "combined": {}},
        "cash_balance": {
            "kis_krw": 45000,
            "kis_krw_fmt": "4.5만",
            "toss_krw": None,
            "toss_krw_fmt": "수동 관리",
            "total_krw": 45000,
            "total_krw_fmt": "4.5만",
        },
        "screening": {"total_scanned": 0, "top_n": 20, "strategy": None, "results": [], "summary": {}},
        "pending_orders": {"total": 0, "buy_count": 0, "sell_count": 0, "orders": []},
        "brief_text": "ok",
        "errors": [],
    }

    with patch(
        "app.routers.n8n.fetch_kr_morning_report",
        new_callable=AsyncMock,
        return_value=payload,
    ):
        response = client.get("/api/n8n/kr-morning-report")

    assert response.status_code == 200
    assert response.json()["cash_balance"]["toss_krw_fmt"] == "수동 관리"
```

**Step 2: Run the API test to verify it fails**

Run: `uv run pytest tests/test_n8n_api.py -k "kr_morning_report_success" -v --no-header`

Expected: FAIL because the route/import is missing.

**Step 3: Add the route**

In `app/routers/n8n.py`:

1. Import `N8nKrMorningReportResponse`.
2. Import `fetch_kr_morning_report`.
3. Add:

```python
@router.get("/kr-morning-report", response_model=N8nKrMorningReportResponse)
async def get_kr_morning_report(
    include_screen: bool = Query(True),
    screen_strategy: str | None = Query(None),
    include_pending: bool = Query(True),
    top_n: int = Query(20, ge=1, le=50),
) -> N8nKrMorningReportResponse | JSONResponse:
    as_of_dt = now_kst().replace(microsecond=0)
    try:
        result = await fetch_kr_morning_report(
            include_screen=include_screen,
            screen_strategy=screen_strategy,
            include_pending=include_pending,
            top_n=top_n,
            as_of=as_of_dt,
        )
    except Exception as exc:
        logger.exception("Failed to build KR morning report")
        payload = N8nKrMorningReportResponse(
            success=False,
            as_of=as_of_dt.isoformat(),
            date_fmt=fmt_date_with_weekday(as_of_dt),
            brief_text="",
            errors=[{"error": str(exc)}],
        )
        return JSONResponse(status_code=500, content=payload.model_dump())

    return N8nKrMorningReportResponse(**result)
```

Do not touch `app/main.py`; `n8n.router` is already registered.

**Step 4: Run the API test**

Run: `uv run pytest tests/test_n8n_api.py -k "kr_morning_report_success" -v --no-header`

Expected: PASS.

**Step 5: Add one more API failure-path test**

Add:

```python
def test_get_kr_morning_report_returns_500_on_service_error(client):
    with patch(
        "app.routers.n8n.fetch_kr_morning_report",
        new_callable=AsyncMock,
        side_effect=Exception("boom"),
    ):
        response = client.get("/api/n8n/kr-morning-report")

    assert response.status_code == 500
    assert response.json()["success"] is False
```

**Step 6: Run both endpoint tests**

Run: `uv run pytest tests/test_n8n_api.py -k "kr_morning_report" -v --no-header`

Expected: PASS.

**Step 7: Commit**

```bash
git add app/routers/n8n.py tests/test_n8n_api.py
git commit -m "feat(n8n): add KR morning report endpoint"
```

---

### Task 6: Document the new endpoint

**Files:**
- Modify: `docs/n8n-api-reference.md`

**Step 1: Add a failing documentation check**

Use a grep check before editing:

Run: `rg -n "/api/n8n/kr-morning-report|KR Morning Report" docs/n8n-api-reference.md`

Expected: no matches.

**Step 2: Update the API reference**

Add:

- one summary row in the top table
- a dedicated section for `GET /api/n8n/kr-morning-report`
- query parameter table
- curl example
- a short note that Toss cash is manual in this version

Suggested note:

```md
> Note: `cash_balance.toss_krw` is `null` in the current version and `toss_krw_fmt` is `"수동 관리"`.
```

**Step 3: Verify the documentation update**

Run: `rg -n "/api/n8n/kr-morning-report|수동 관리" docs/n8n-api-reference.md`

Expected: matching lines printed.

**Step 4: Commit**

```bash
git add docs/n8n-api-reference.md
git commit -m "docs(n8n): document KR morning report endpoint"
```

---

### Task 7: Run verification and manual smoke checks

**Files:**
- Check: `app/schemas/n8n.py`
- Check: `app/services/n8n_kr_morning_report_service.py`
- Check: `app/routers/n8n.py`
- Check: `tests/test_n8n_kr_morning_report.py`
- Check: `tests/test_n8n_api.py`

**Step 1: Run the focused test suite**

Run:

```bash
uv run pytest \
  tests/test_n8n_kr_morning_report.py \
  tests/test_n8n_api.py -k "kr_morning_report" \
  -v --no-header
```

Expected: all KR morning report tests PASS.

**Step 2: Run lint on touched files**

Run:

```bash
uv run ruff check \
  app/schemas/n8n.py \
  app/services/n8n_kr_morning_report_service.py \
  app/routers/n8n.py \
  tests/test_n8n_kr_morning_report.py \
  tests/test_n8n_api.py \
  docs/n8n-api-reference.md
```

Expected: no lint errors from Python files. If Ruff complains about the Markdown file path, rerun without the doc path.

**Step 3: Run formatting check**

Run:

```bash
uv run ruff format --check \
  app/schemas/n8n.py \
  app/services/n8n_kr_morning_report_service.py \
  app/routers/n8n.py \
  tests/test_n8n_kr_morning_report.py \
  tests/test_n8n_api.py
```

Expected: all files already formatted.

**Step 4: Run manual endpoint smoke check**

Run:

```bash
curl -s http://127.0.0.1:8000/api/n8n/kr-morning-report \
  -H "X-N8N-API-KEY: $N8N_API_KEY" | jq '{success, date_fmt, cash_balance, brief_text}'
```

Expected:

- `success: true`
- `cash_balance.kis_krw` is numeric
- `cash_balance.toss_krw` is `null`
- `cash_balance.toss_krw_fmt` is `"수동 관리"`
- `brief_text` is non-empty

**Step 5: Optional live sanity check**

If local credentials are configured, verify:

```bash
curl -s http://127.0.0.1:8000/api/n8n/kr-morning-report?include_screen=false \
  -H "X-N8N-API-KEY: $N8N_API_KEY" | jq '.screening,.pending_orders'
```

Expected:

- `screening.results == []`
- `pending_orders` still present when `include_pending=true`

**Step 6: Final commit**

```bash
git add \
  app/schemas/n8n.py \
  app/services/n8n_kr_morning_report_service.py \
  app/routers/n8n.py \
  tests/test_n8n_kr_morning_report.py \
  tests/test_n8n_api.py \
  docs/n8n-api-reference.md
git commit -m "feat(n8n): add KR morning report endpoint"
```

---

## Out of Scope

- Creating or exporting the n8n workflow itself
- Discord thread orchestration code in this repo
- Waking OpenClaw from this backend
- Automatic Toss cash ingestion
- New auth or middleware changes

## Execution Notes

- Prefer reusing helpers from `n8n_formatting.py` instead of adding new formatting rules unless the response contract requires a new helper.
- If `screen_stocks_impl()` field names differ from the assumptions above, normalize them in a single helper and lock the mapping down with tests instead of leaking raw upstream keys into the API contract.
- Keep the route thin. Any non-trivial grouping, sorting, text-building, or error aggregation belongs in the service file.

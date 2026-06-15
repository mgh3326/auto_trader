# ROB-577 My Orders Follow-Up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the ROB-577 follow-up for `/invest/my`: symbol links from order/fill rows, a dedicated buy-history tab, current-order symbol-name enrichment, KIS KR order time repair, and market-order price labels.

**Architecture:** Keep the existing sell-history panel intact because its totals and realized P/L columns are sell-specific. Add buy history as a separate read-only panel backed by `/fills/recent?side=buy`, and make the side filter happen in SQL before the limit. For current open orders, inject the request DB session into `CurrentOrdersService` only for best-effort display-name enrichment; lookup failures must not degrade the broker source state or drop rows.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async sessions, Pydantic, pytest, React 19, React Router 7, TypeScript, Vitest, Testing Library, `uv`, `npm`.

---

## Scope And Decisions

- Linear issue: `ROB-577`
- Status at planning time: `Backlog`
- Migration: none
- Model lane: `keep_on_gpt54`
- Risk classification: not `high_risk_change`; this is read-only display/query work and does not change live order execution boundaries.
- Product decisions:
  - New tab is a standalone `매수 이력` tab.
  - `/fills/recent` gets an additive `side=buy|sell` server filter.
  - Current open orders display `시장가` or `order_type` when `price` is null.

## File Structure

- Modify `app/routers/invest_fills.py`: add `side` query parameter to `/recent`.
- Modify `app/services/execution_ledger/query_service.py`: add `side` argument to `list_recent` and apply `ExecutionLedger.side == side` before SQL limit.
- Modify `tests/routers/test_invest_fills_router.py`: pin `/recent?side=buy` response and invalid side rejection.
- Modify `frontend/invest/src/api/fills.ts`: add `side` option to `fetchRecentFills`.
- Add `frontend/invest/src/__tests__/fills.api.test.ts`: cover `side=buy` URL generation and credentials.
- Modify `frontend/invest/src/stockDetailPath.ts`: widen `stockDetailPath` to route market keys.
- Modify `frontend/invest/src/__tests__/stockDetailPath.test.ts`: cover lowercase market support on `stockDetailPath`.
- Modify `frontend/invest/src/components/my/CurrentOrdersPanel.tsx`: link symbols and show market-order labels.
- Modify `frontend/invest/src/__tests__/CurrentOrdersPanel.test.tsx`: wrap in router, assert links and market-order label.
- Modify `frontend/invest/src/components/my/SellHistoryPanel.tsx`: link sell-history symbols without changing sell-specific totals/P/L behavior.
- Modify `frontend/invest/src/__tests__/SellHistoryPanel.test.tsx`: wrap in router and assert the sell row link.
- Add `frontend/invest/src/components/my/BuyHistoryPanel.tsx`: dedicated buy-history table using `fetchRecentFills(..., side: "buy")`.
- Add `frontend/invest/src/__tests__/BuyHistoryPanel.test.tsx`: cover rendering, credentials, market refetch, and stock links.
- Modify `frontend/invest/src/components/my/portfolioTabs.ts`: add `buyHistory`.
- Modify `frontend/invest/src/pages/desktop/DesktopPortfolioPage.tsx`: title/description/render branch for `buyHistory`.
- Modify `frontend/invest/src/pages/mobile/MobilePortfolioPage.tsx`: render `BuyHistoryPanel compact`.
- Modify `app/routers/invest_open_orders.py`: inject DB session into `CurrentOrdersService`.
- Modify `app/services/current_orders_service.py`: add session-backed name enrichment and KIS KR `ord_tmd`-only parsing.
- Modify `tests/test_current_orders_service.py`: pin enrichment, fail-open lookup behavior, and KIS KR date synthesis.

### Task 1: Backend `/fills/recent` Side Filter

**Files:**
- Modify: `app/routers/invest_fills.py`
- Modify: `app/services/execution_ledger/query_service.py`
- Test: `tests/routers/test_invest_fills_router.py`

- [ ] **Step 1: Write router tests for side filtering**

Append these tests near the `/recent` section in `tests/routers/test_invest_fills_router.py`:

```python
@pytest.mark.unit
def test_recent_fills_accepts_side_filter():
    buy = _ledger_row(id=1, side="buy", broker_order_id="buy-1")
    run = _reconcile_run_row("kis")
    db = _make_db([buy], [run])
    client = TestClient(_make_app(db))

    resp = client.get("/trading/api/invest/fills/recent?side=buy")

    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["items"][0]["side"] == "buy"
    assert data["items"][0]["broker_order_id"] == "buy-1"


@pytest.mark.unit
def test_recent_fills_rejects_unknown_side():
    db = _make_db([], [])
    client = TestClient(_make_app(db))

    resp = client.get("/trading/api/invest/fills/recent?side=hold")

    assert resp.status_code == 422
```

The query assertion is pinned in the next step with a service-level test.

- [ ] **Step 2: Write query-service test proving side is applied before limit**

Add this test to `tests/routers/test_invest_fills_router.py` after the router side-filter test:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_recent_fills_side_filter_is_applied_before_limit():
    older_buy = _ledger_row(
        id=2,
        side="buy",
        broker_order_id="buy-old",
        filled_at=datetime(2026, 5, 10, 9, 0, tzinfo=UTC),
    )

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self

        def all(self):
            return self._rows

    executed = []

    async def _execute(stmt):
        executed.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
        if len(executed) == 1:
            return _Result([older_buy])
        return _Result([_reconcile_run_row("kis")])

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)

    from app.services.execution_ledger.query_service import ExecutionLedgerQueryService

    response = await ExecutionLedgerQueryService(db).list_recent(limit=1, side="buy")

    assert response.count == 1
    assert response.items[0].broker_order_id == "buy-old"
    assert "execution_ledger.side = 'buy'" in executed[0]
    assert "LIMIT 3" in executed[0]
```

- [ ] **Step 3: Run the new backend tests and confirm failure**

Run:

```bash
uv run pytest tests/routers/test_invest_fills_router.py::test_recent_fills_accepts_side_filter tests/routers/test_invest_fills_router.py::test_recent_fills_rejects_unknown_side tests/routers/test_invest_fills_router.py::test_recent_fills_side_filter_is_applied_before_limit -q
```

Expected: `test_recent_fills_accepts_side_filter` fails because `/recent` does not accept or pass `side`; `test_recent_fills_side_filter_is_applied_before_limit` fails because `list_recent` does not accept `side`.

- [ ] **Step 4: Add the side filter implementation**

In `app/routers/invest_fills.py`, change the imports and `/recent` endpoint to:

```python
from app.schemas.execution_ledger import (
    ExecutionLedgerFreshnessReport,
    ExecutionLedgerListResponse,
    Side,
)


@router.get("/recent")
async def recent_fills(
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    market: Market | None = None,
    side: Side | None = None,
) -> ExecutionLedgerListResponse:
    return await ExecutionLedgerQueryService(db).list_recent(
        limit=limit,
        market=market,
        side=side,
    )
```

In `app/services/execution_ledger/query_service.py`, update `list_recent`:

```python
    async def list_recent(
        self, *, limit: int = 50, market: str | None = None, side: str | None = None
    ) -> ExecutionLedgerListResponse:
        # Over-fetch before de-dup so superseded websocket rows do not consume the
        # page budget (otherwise a dup-heavy page returns fewer than `limit` rows).
        # 3x covers the worst-case number of sources for one order.
        stmt = (
            select(ExecutionLedger)
            .order_by(ExecutionLedger.filled_at.desc())
            .limit(limit * 3)
        )
        stmt = ExecutionLedgerRepository.apply_market_filter(stmt, market)
        if side is not None:
            stmt = stmt.where(ExecutionLedger.side == side)
        rows = (await self.db.execute(stmt)).scalars().all()
        items = [ExecutionLedgerRead.model_validate(row) for row in rows]
        items = _supersede_provisional_fills(items)[:limit]
        items = await self._attach_symbol_names(items)
```

- [ ] **Step 5: Re-run the targeted backend tests**

Run:

```bash
uv run pytest tests/routers/test_invest_fills_router.py::test_recent_fills_accepts_side_filter tests/routers/test_invest_fills_router.py::test_recent_fills_rejects_unknown_side tests/routers/test_invest_fills_router.py::test_recent_fills_side_filter_is_applied_before_limit -q
```

Expected: all three tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add app/routers/invest_fills.py app/services/execution_ledger/query_service.py tests/routers/test_invest_fills_router.py
git commit -m "feat: filter recent fills by side"
```

### Task 2: Frontend Fills API Side Parameter

**Files:**
- Modify: `frontend/invest/src/api/fills.ts`
- Test: `frontend/invest/src/__tests__/fills.api.test.ts`

- [ ] **Step 1: Add API tests**

Create `frontend/invest/src/__tests__/fills.api.test.ts`:

```ts
import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchRecentFills } from "../api/fills";

const originalFetch = global.fetch;

afterEach(() => {
  global.fetch = originalFetch;
});

describe("fetchRecentFills", () => {
  it("calls recent fills with credentials and default limit", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ count: 0, items: [], data_state: "fresh", source_breakdown: null, empty_reason: null }),
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    await fetchRecentFills();

    expect(fetchMock).toHaveBeenCalledWith(
      "/trading/api/invest/fills/recent?limit=50",
      { credentials: "include" },
    );
  });

  it("passes market and side when provided", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ count: 0, items: [], data_state: "fresh", source_breakdown: null, empty_reason: null }),
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    await fetchRecentFills(30, "kr", "buy");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/trading/api/invest/fills/recent?limit=30&market=kr&side=buy");
    expect(init.credentials).toBe("include");
  });

  it("throws on non-ok responses", async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: false,
      status: 500,
    }) as unknown as typeof fetch;

    await expect(fetchRecentFills(10, undefined, "buy")).rejects.toThrow("fills/recent 500");
  });
});
```

- [ ] **Step 2: Run the API tests and confirm failure**

Run:

```bash
npm --prefix frontend/invest test -- src/__tests__/fills.api.test.ts
```

Expected: the second test fails because `fetchRecentFills` does not accept or append `side`.

- [ ] **Step 3: Add the side parameter**

Modify `frontend/invest/src/api/fills.ts`:

```ts
import type {
  FillListResponse,
  FillMarket,
  FillFreshnessReport,
  FillSide,
} from "../types/fills";

const BASE = "/trading/api/invest/fills";

export async function fetchRecentFills(
  limit = 50,
  market?: FillMarket,
  side?: FillSide,
): Promise<FillListResponse> {
  const q = new URLSearchParams({ limit: String(limit) });
  if (market) q.set("market", market);
  if (side) q.set("side", side);
  const res = await fetch(`${BASE}/recent?${q}`, { credentials: "include" });
  if (!res.ok) throw new Error(`fills/recent ${res.status}`);
  return res.json();
}
```

- [ ] **Step 4: Re-run the API tests**

Run:

```bash
npm --prefix frontend/invest test -- src/__tests__/fills.api.test.ts
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add frontend/invest/src/api/fills.ts frontend/invest/src/__tests__/fills.api.test.ts
git commit -m "feat: add fill side query to frontend api"
```

### Task 3: Stock Detail Path And Existing Row Links

**Files:**
- Modify: `frontend/invest/src/stockDetailPath.ts`
- Modify: `frontend/invest/src/components/my/CurrentOrdersPanel.tsx`
- Modify: `frontend/invest/src/components/my/SellHistoryPanel.tsx`
- Test: `frontend/invest/src/__tests__/stockDetailPath.test.ts`
- Test: `frontend/invest/src/__tests__/CurrentOrdersPanel.test.tsx`
- Test: `frontend/invest/src/__tests__/SellHistoryPanel.test.tsx`

- [ ] **Step 1: Add path and link tests**

In `frontend/invest/src/__tests__/stockDetailPath.test.ts`, add:

```ts
test("stock detail path supports lowercase route market keys", () => {
  expect(stockDetailPath("kr", "005930")).toBe("/stocks/kr/005930");
  expect(stockDetailPath("us", "BRK-B")).toBe("/stocks/us/BRK-B");
  expect(stockDetailPath("crypto", "BTC")).toBe("/stocks/crypto/KRW-BTC");
});
```

In `frontend/invest/src/__tests__/CurrentOrdersPanel.test.tsx`, change the top import to include `MemoryRouter`:

```ts
import { MemoryRouter } from "react-router-dom";
```

Add a helper after `afterEach`:

```tsx
function renderCurrentOrdersPanel(compact = false) {
  return render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/my"]}>
      <CurrentOrdersPanel compact={compact} />
    </MemoryRouter>,
  );
}
```

Replace direct renders:

```tsx
render(<CurrentOrdersPanel />);
```

with:

```tsx
renderCurrentOrdersPanel();
```

Replace:

```tsx
render(<CurrentOrdersPanel compact />);
```

with:

```tsx
renderCurrentOrdersPanel(true);
```

Add this test:

```tsx
test("CurrentOrdersPanel links symbols to stock detail and labels market orders", async () => {
  fetchMock.mockResolvedValue({
    ok: true,
    json: async () => ({
      ...baseResponse,
      items: [
        {
          ...baseResponse.items[0],
          price: null,
          order_type: "시장가",
        },
      ],
    }),
  });

  renderCurrentOrdersPanel();

  const link = await screen.findByRole("link", { name: /삼성전자/ });
  expect(link).toHaveAttribute("href", "/invest/stocks/kr/005930");
  expect(screen.getByText("시장가")).toBeInTheDocument();
});
```

In `frontend/invest/src/__tests__/SellHistoryPanel.test.tsx`, import `MemoryRouter`, add:

```tsx
function renderSellHistoryPanel(compact = false) {
  return render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/my?tab=sellHistory"]}>
      <SellHistoryPanel compact={compact} />
    </MemoryRouter>,
  );
}
```

Replace direct `render(<SellHistoryPanel />)` calls with `renderSellHistoryPanel()`.

Add:

```tsx
test("SellHistoryPanel links rows to stock detail", async () => {
  renderSellHistoryPanel();

  const link = await screen.findByRole("link", { name: /SK하이닉스/ });
  expect(link).toHaveAttribute("href", "/invest/stocks/kr/000660");
});
```

- [ ] **Step 2: Run link tests and confirm failure**

Run:

```bash
npm --prefix frontend/invest test -- src/__tests__/stockDetailPath.test.ts src/__tests__/CurrentOrdersPanel.test.tsx src/__tests__/SellHistoryPanel.test.tsx
```

Expected: lowercase `stockDetailPath` fails at type/runtime implementation; link assertions fail because rows are not links yet.

- [ ] **Step 3: Widen `stockDetailPath`**

Modify `frontend/invest/src/stockDetailPath.ts`:

```ts
export function stockDetailMarketParam(market: RouteMarket): StockDetailMarketParam {
  return routeMarketParam(market);
}

export function stockDetailPath(market: RouteMarket, symbol: string): string | null {
  const cleanSymbol = symbol.trim();
  if (!cleanSymbol) return null;
  const marketParam = routeMarketParam(market);
  return `/stocks/${marketParam}/${encodeURIComponent(stockDetailRouteSymbol(market, cleanSymbol))}`;
}
```

- [ ] **Step 4: Link current-order rows and label market orders**

Modify `frontend/invest/src/components/my/CurrentOrdersPanel.tsx`.

Add import:

```ts
import { Link } from "react-router-dom";
import { stockDetailPath } from "../../stockDetailPath";
```

Add helper near `formatMoney`:

```ts
function formatOrderPrice(row: CurrentOrderRow): string {
  if (row.price != null && row.price !== "") return formatMoney(row.price, row.currency);
  const orderType = row.order_type?.trim();
  if (!orderType) return "—";
  const normalized = orderType.toLowerCase();
  if (normalized === "market" || orderType.includes("시장")) return "시장가";
  return orderType;
}
```

Inside the `rows.map`, change to a block so `href` can be computed:

```tsx
{rows.map((row) => {
  const href = stockDetailPath(row.market, row.symbol);
  const name = symbolName(row);
  const symbolBlock = (
    <>
      <div style={{ fontSize: 13, fontWeight: 800 }}>{name}</div>
      <div style={{ marginTop: 2, fontSize: 11, color: "var(--fg-3)" }}>
        {row.symbol} · {MARKET_LABEL[row.market]}{row.exchange ? ` · ${row.exchange}` : ""}
      </div>
    </>
  );

  return (
    <tr key={`${row.broker}:${row.market}:${row.order_no}`}>
      ...
      <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)" }}>
        {href ? (
          <Link to={href} style={{ color: "inherit", textDecoration: "none" }}>
            {symbolBlock}
          </Link>
        ) : (
          symbolBlock
        )}
      </td>
      ...
      <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 13, textAlign: "right", fontFeatureSettings: '"tnum"' }}>
        {formatOrderPrice(row)}
      </td>
      ...
    </tr>
  );
})}
```

Keep the existing status, quantity, broker, and footer rendering unchanged.

- [ ] **Step 5: Link sell-history rows**

Modify `frontend/invest/src/components/my/SellHistoryPanel.tsx`.

Add imports:

```ts
import { Link } from "react-router-dom";
import { stockDetailPath } from "../../stockDetailPath";
```

Add helper near `symbolDisplayName`:

```ts
function routeMarket(row: FillRow): "kr" | "us" | "crypto" | null {
  if (row.instrument_type === "equity_kr") return "kr";
  if (row.instrument_type === "equity_us") return "us";
  if (row.instrument_type === "crypto") return "crypto";
  return null;
}
```

Inside each row render:

```tsx
const market = routeMarket(row);
const href = market ? stockDetailPath(market, row.symbol) : null;
const symbolBlock = (
  <>
    <div style={{ fontSize: 13, fontWeight: 800 }}>{displayName ?? row.symbol}</div>
    <div style={{ marginTop: 2, fontSize: 11, color: "var(--fg-3)" }}>
      {compact
        ? `${row.symbol} · ${formatQty(row)}`
        : `${row.symbol} · ${row.broker.toUpperCase()} · ${row.venue}`}
    </div>
  </>
);
```

Replace the symbol cell contents with:

```tsx
{href ? (
  <Link to={href} style={{ color: "inherit", textDecoration: "none" }}>
    {symbolBlock}
  </Link>
) : (
  symbolBlock
)}
```

- [ ] **Step 6: Re-run frontend link tests**

Run:

```bash
npm --prefix frontend/invest test -- src/__tests__/stockDetailPath.test.ts src/__tests__/CurrentOrdersPanel.test.tsx src/__tests__/SellHistoryPanel.test.tsx
```

Expected: all tests pass.

- [ ] **Step 7: Commit Task 3**

```bash
git add frontend/invest/src/stockDetailPath.ts frontend/invest/src/components/my/CurrentOrdersPanel.tsx frontend/invest/src/components/my/SellHistoryPanel.tsx frontend/invest/src/__tests__/stockDetailPath.test.ts frontend/invest/src/__tests__/CurrentOrdersPanel.test.tsx frontend/invest/src/__tests__/SellHistoryPanel.test.tsx
git commit -m "feat: link order and fill rows to stock detail"
```

### Task 4: Dedicated Buy History Tab

**Files:**
- Create: `frontend/invest/src/components/my/BuyHistoryPanel.tsx`
- Create: `frontend/invest/src/__tests__/BuyHistoryPanel.test.tsx`
- Modify: `frontend/invest/src/components/my/portfolioTabs.ts`
- Modify: `frontend/invest/src/pages/desktop/DesktopPortfolioPage.tsx`
- Modify: `frontend/invest/src/pages/mobile/MobilePortfolioPage.tsx`
- Test: `frontend/invest/src/__tests__/CurrentOrdersPanel.test.tsx`

- [ ] **Step 1: Add buy-history panel tests**

Create `frontend/invest/src/__tests__/BuyHistoryPanel.test.tsx`:

```tsx
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { BuyHistoryPanel } from "../components/my/BuyHistoryPanel";

const fetchMock = vi.fn();

const baseResponse = {
  count: 1,
  data_state: "fresh",
  empty_reason: null,
  source_breakdown: { reconciler: 1, websocket: 0, manual_import: 0 },
  items: [
    {
      id: 7,
      broker: "kis",
      account_mode: "live",
      venue: "krx",
      instrument_type: "equity_kr",
      symbol: "005930",
      raw_symbol: "005930",
      symbol_name: "삼성전자",
      side: "buy",
      broker_order_id: "0006421201",
      fill_seq: 733331393,
      filled_qty: "2.00000000",
      filled_price: "70000.00000000",
      filled_notional: "140000.0000",
      fee_amount: "0.0",
      fee_currency: "KRW",
      filled_at: "2026-06-15T00:01:09Z",
      currency: "KRW",
      correlation_id: null,
      source: "reconciler",
      source_run_id: "run-1",
      created_at: "2026-06-15T00:02:00Z",
      updated_at: null,
    },
  ],
};

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue({ ok: true, json: async () => baseResponse });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderBuyHistoryPanel(compact = false) {
  return render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/my?tab=buyHistory"]}>
      <BuyHistoryPanel compact={compact} />
    </MemoryRouter>,
  );
}

test("BuyHistoryPanel renders buy fills and calls side-filtered endpoint", async () => {
  renderBuyHistoryPanel();

  expect(await screen.findByText("삼성전자")).toBeInTheDocument();
  expect(screen.getByText("매수 이력")).toBeInTheDocument();
  expect(screen.getByText("총 매수금액 · KRW")).toBeInTheDocument();
  expect(screen.getAllByText("₩140,000").length).toBeGreaterThan(0);
  expect(screen.getByText("출처 보정 1")).toBeInTheDocument();

  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(url).toContain("/trading/api/invest/fills/recent");
  expect(url).toContain("limit=30");
  expect(url).toContain("side=buy");
  expect(init.credentials).toBe("include");
});

test("BuyHistoryPanel refetches with market filter", async () => {
  renderBuyHistoryPanel();
  await screen.findByText("삼성전자");

  await userEvent.click(screen.getByRole("button", { name: "국내" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  const [url] = fetchMock.mock.calls[1] as [string, RequestInit];
  expect(url).toContain("market=kr");
  expect(url).toContain("side=buy");
});

test("BuyHistoryPanel links symbols to stock detail", async () => {
  renderBuyHistoryPanel();

  const link = await screen.findByRole("link", { name: /삼성전자/ });
  expect(link).toHaveAttribute("href", "/invest/stocks/kr/005930");
});

test("BuyHistoryPanel renders empty reason", async () => {
  fetchMock.mockResolvedValue({
    ok: true,
    json: async () => ({
      ...baseResponse,
      count: 0,
      items: [],
      empty_reason: "no buy fills in the requested window",
    }),
  });

  renderBuyHistoryPanel(true);

  expect(await screen.findByText("no buy fills in the requested window")).toBeInTheDocument();
});
```

- [ ] **Step 2: Extend portfolio tab tests**

In `frontend/invest/src/__tests__/CurrentOrdersPanel.test.tsx`, update `TabProbe`:

```tsx
function TabProbe() {
  const [activeTab, setActiveTab] = usePortfolioTabSearchParam();
  return (
    <>
      <div data-testid="active-tab">{activeTab}</div>
      <button type="button" onClick={() => setActiveTab("currentOrders")}>set current</button>
      <button type="button" onClick={() => setActiveTab("buyHistory")}>set buy</button>
    </>
  );
}
```

Add:

```tsx
test("portfolio tabs include buy history and parse the search param", async () => {
  expect(PORTFOLIO_TABS.map((tab) => tab.key)).toContain("buyHistory");
  render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/my?tab=buyHistory"]}>
      <TabProbe />
    </MemoryRouter>,
  );
  expect(screen.getByTestId("active-tab")).toHaveTextContent("buyHistory");
});
```

- [ ] **Step 3: Run buy-history tests and confirm failure**

Run:

```bash
npm --prefix frontend/invest test -- src/__tests__/BuyHistoryPanel.test.tsx src/__tests__/CurrentOrdersPanel.test.tsx
```

Expected: `BuyHistoryPanel` import fails and `buyHistory` tab parsing fails.

- [ ] **Step 4: Create `BuyHistoryPanel.tsx`**

Create `frontend/invest/src/components/my/BuyHistoryPanel.tsx`:

```tsx
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { fetchRecentFills } from "../../api/fills";
import { stockDetailPath } from "../../stockDetailPath";
import type { FillListResponse, FillMarket, FillRow } from "../../types/fills";

const MARKET_OPTIONS: { key: FillMarket | "all"; label: string }[] = [
  { key: "all", label: "전체" },
  { key: "kr", label: "국내" },
  { key: "us", label: "미국" },
  { key: "crypto", label: "코인" },
];

function toNumber(value: string | number | null | undefined): number | null {
  if (value == null || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatMoney(value: string | number | null | undefined, currency: string): string {
  const n = toNumber(value);
  if (n == null) return "—";
  if (currency === "USD") {
    return `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }
  if (currency === "KRW") return `₩${Math.round(n).toLocaleString("ko-KR")}`;
  return `${n.toLocaleString("ko-KR")} ${currency}`;
}

function formatQty(row: FillRow): string {
  const qty = toNumber(row.filled_qty);
  if (qty == null) return "—";
  if (row.instrument_type === "crypto") return qty.toLocaleString("ko-KR", { maximumFractionDigits: 8 });
  return `${qty.toLocaleString("ko-KR", { maximumFractionDigits: 4 })}주`;
}

function formatDateTime(value: string): string {
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Seoul",
  }).format(dt);
}

function sourceLabel(row: FillRow): string {
  if (row.source === "websocket") return "실시간";
  if (row.source === "reconciler") return "보정";
  if (row.source === "manual_import") return "수동";
  return row.source;
}

function sourceBreakdownLabel(data: FillListResponse): string | null {
  const breakdown = data.source_breakdown;
  if (!breakdown) return null;
  const parts = [
    ["실시간", breakdown.websocket],
    ["보정", breakdown.reconciler],
    ["수동", breakdown.manual_import],
  ].filter(([, count]) => Number(count) > 0);
  if (parts.length === 0) return null;
  return parts.map(([label, count]) => `${label} ${count}`).join(" · ");
}

function symbolDisplayName(row: FillRow): string | null {
  const name = row.symbol_name ?? row.symbolName;
  if (!name || name === row.symbol) return null;
  return name;
}

function routeMarket(row: FillRow): "kr" | "us" | "crypto" | null {
  if (row.instrument_type === "equity_kr") return "kr";
  if (row.instrument_type === "equity_us") return "us";
  if (row.instrument_type === "crypto") return "crypto";
  return null;
}

function totalByCurrency(rows: FillRow[]): { currency: string; total: number }[] {
  const totals = new Map<string, number>();
  for (const row of rows) {
    const notional = toNumber(row.filled_notional);
    if (notional == null) continue;
    totals.set(row.currency, (totals.get(row.currency) ?? 0) + notional);
  }
  return Array.from(totals.entries()).map(([currency, total]) => ({ currency, total }));
}

export function BuyHistoryPanel({ compact = false }: { compact?: boolean }) {
  const [market, setMarket] = useState<FillMarket | "all">("all");
  const [state, setState] = useState<
    | { status: "loading" }
    | { status: "ready"; data: FillListResponse }
    | { status: "error"; message: string }
  >({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    fetchRecentFills(compact ? 8 : 30, market === "all" ? undefined : market, "buy")
      .then((data) => {
        if (!cancelled) setState({ status: "ready", data });
      })
      .catch((err: unknown) => {
        if (!cancelled) setState({ status: "error", message: err instanceof Error ? err.message : String(err) });
      });
    return () => {
      cancelled = true;
    };
  }, [market, compact]);

  const rows = useMemo(() => (state.status === "ready" ? state.data.items : []), [state]);
  const count = state.status === "ready" ? state.data.count : 0;
  const dataState = state.status === "ready" ? state.data.data_state : null;
  const breakdownLabel = state.status === "ready" ? sourceBreakdownLabel(state.data) : null;
  const buyTotals = useMemo(() => totalByCurrency(rows), [rows]);

  return (
    <section
      data-testid="buy-history-panel"
      style={{
        border: "1px solid var(--border)",
        borderRadius: 16,
        background: "var(--surface)",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: compact ? "flex-start" : "center",
          justifyContent: "space-between",
          gap: 12,
          padding: compact ? "14px 14px 10px" : "16px 18px 12px",
          flexDirection: compact ? "column" : "row",
        }}
      >
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <h2 style={{ margin: 0, fontSize: compact ? 16 : 18, letterSpacing: "-0.02em" }}>매수 이력</h2>
            {dataState && (
              <span
                style={{
                  padding: "2px 7px",
                  borderRadius: 999,
                  fontSize: 11,
                  fontWeight: 700,
                  color: dataState === "fresh" ? "var(--gain)" : "var(--warn)",
                  background: dataState === "fresh" ? "var(--gain-soft)" : "var(--warn-soft)",
                }}
              >
                {dataState === "fresh" ? "최신" : dataState === "stale" ? "지연" : "대기"}
              </span>
            )}
          </div>
          <p style={{ margin: "4px 0 0", fontSize: 12, color: "var(--fg-3)" }}>
            KIS/Upbit 체결 보정 ledger 기준 최근 매수 체결입니다.
          </p>
          {breakdownLabel && (
            <p style={{ margin: "4px 0 0", fontSize: 11, color: "var(--fg-3)" }}>
              출처 {breakdownLabel}
            </p>
          )}
        </div>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {MARKET_OPTIONS.map((option) => {
            const active = market === option.key;
            return (
              <button
                key={option.key}
                type="button"
                onClick={() => setMarket(option.key)}
                style={{
                  border: "none",
                  borderRadius: 999,
                  padding: "6px 10px",
                  fontSize: 12,
                  fontWeight: 700,
                  cursor: "pointer",
                  fontFamily: "inherit",
                  background: active ? "var(--fg)" : "var(--surface-2)",
                  color: active ? "var(--bg)" : "var(--fg-2)",
                }}
              >
                {option.label}
              </button>
            );
          })}
        </div>
      </div>

      {buyTotals.length > 0 && (
        <div
          style={{
            display: "flex",
            gap: 8,
            flexWrap: "wrap",
            padding: compact ? "0 14px 12px" : "0 18px 14px",
          }}
          aria-label="매수 금액 요약"
        >
          {buyTotals.map(({ currency, total }) => (
            <div
              key={currency}
              style={{
                borderRadius: 12,
                background: "var(--surface-2)",
                padding: "8px 10px",
                minWidth: compact ? 0 : 150,
              }}
            >
              <div style={{ fontSize: 10, color: "var(--fg-3)", fontWeight: 700 }}>총 매수금액 · {currency}</div>
              <div style={{ marginTop: 2, fontSize: compact ? 13 : 15, fontWeight: 900, fontFeatureSettings: '"tnum"' }}>
                {formatMoney(total, currency)}
              </div>
            </div>
          ))}
        </div>
      )}

      {state.status === "loading" && (
        <div style={{ padding: 24, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>매수 이력을 불러오는 중…</div>
      )}

      {state.status === "error" && (
        <div role="alert" style={{ padding: 16, color: "var(--danger)", fontSize: 13 }}>
          매수 이력을 불러오지 못했습니다. {state.message}
        </div>
      )}

      {state.status === "ready" && rows.length === 0 && (
        <div style={{ padding: 24, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>
          {state.data.empty_reason ?? "최근 매수 체결이 없습니다."}
        </div>
      )}

      {state.status === "ready" && rows.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", minWidth: compact ? 0 : 760 }}>
            <thead>
              <tr style={{ color: "var(--fg-3)", fontSize: 11, textAlign: "left" }}>
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>일시</th>
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>종목</th>
                {!compact && <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>수량</th>}
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)", textAlign: "right" }}>단가</th>
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)", textAlign: "right" }}>총 매수금액</th>
                {!compact && <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>출처</th>}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const displayName = symbolDisplayName(row);
                const marketParam = routeMarket(row);
                const href = marketParam ? stockDetailPath(marketParam, row.symbol) : null;
                const symbolBlock = (
                  <>
                    <div style={{ fontSize: 13, fontWeight: 800 }}>{displayName ?? row.symbol}</div>
                    <div style={{ marginTop: 2, fontSize: 11, color: "var(--fg-3)" }}>
                      {compact
                        ? `${row.symbol} · ${formatQty(row)}`
                        : `${row.symbol} · ${row.broker.toUpperCase()} · ${row.venue}`}
                    </div>
                  </>
                );
                return (
                  <tr key={`${row.broker}-${row.account_mode}-${row.venue}-${row.broker_order_id}-${row.fill_seq}`}>
                    <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 12, color: "var(--fg-2)", whiteSpace: "nowrap" }}>
                      {formatDateTime(row.filled_at)}
                    </td>
                    <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)" }}>
                      {href ? (
                        <Link to={href} style={{ color: "inherit", textDecoration: "none" }}>
                          {symbolBlock}
                        </Link>
                      ) : (
                        symbolBlock
                      )}
                    </td>
                    {!compact && <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 13 }}>{formatQty(row)}</td>}
                    <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 13, textAlign: "right", fontFeatureSettings: '"tnum"' }}>
                      {formatMoney(row.filled_price, row.currency)}
                    </td>
                    <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 13, fontWeight: 800, textAlign: "right", fontFeatureSettings: '"tnum"' }}>
                      {formatMoney(row.filled_notional, row.currency)}
                    </td>
                    {!compact && <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 12, color: "var(--fg-3)" }}>{sourceLabel(row)}</td>}
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div style={{ padding: "8px 14px", fontSize: 11, color: "var(--fg-3)" }}>
            총 {count.toLocaleString("ko-KR")}건{count > rows.length ? ` 중 ${rows.length}건 표시` : ""}
          </div>
        </div>
      )}
    </section>
  );
}
```

- [ ] **Step 5: Add the tab and page branches**

Modify `frontend/invest/src/components/my/portfolioTabs.ts`:

```ts
export type PortfolioTab = "holdings" | "signals" | "sellHistory" | "buyHistory" | "currentOrders";

export const PORTFOLIO_TABS: { key: PortfolioTab; label: string }[] = [
  { key: "holdings", label: "보유 현황" },
  { key: "signals", label: "시그널" },
  { key: "sellHistory", label: "매도 이력" },
  { key: "buyHistory", label: "매수 이력" },
  { key: "currentOrders", label: "현재 주문" },
];

function parsePortfolioTab(value: string | null): PortfolioTab {
  return value === "signals" || value === "sellHistory" || value === "buyHistory" || value === "currentOrders"
    ? value
    : "holdings";
}
```

Modify `frontend/invest/src/pages/desktop/DesktopPortfolioPage.tsx`:

```ts
import { BuyHistoryPanel } from "../../components/my/BuyHistoryPanel";
```

Update helpers:

```ts
function portfolioTitle(tab: PortfolioTab): string {
  if (tab === "holdings") return "통합 보유 현황";
  if (tab === "signals") return "내 투자 시그널";
  if (tab === "currentOrders") return "현재 주문";
  if (tab === "buyHistory") return "매수 이력";
  return "매도 이력";
}

function portfolioDescription(tab: PortfolioTab): string {
  if (tab === "holdings") return "KIS, Toss/manual, 모의/수동 계좌를 한 화면에서 비교하고 종목별 출처를 확인합니다.";
  if (tab === "signals") return "보유·관심 종목과 시장별 AI 분석 시그널을 내 투자 화면에서 함께 확인합니다.";
  if (tab === "currentOrders") return "KIS/Toss/Upbit 실계좌의 현재 미체결·대기 주문을 읽기 전용으로 확인합니다.";
  if (tab === "buyHistory") return "KIS/Upbit 체결 보정 ledger 기준 최근 매수 체결을 별도 화면에서 확인합니다.";
  return "KIS/Upbit 체결 보정 ledger 기준 최근 매도 체결을 별도 화면에서 확인합니다.";
}
```

Update the render branch:

```tsx
) : activeTab === "currentOrders" ? (
  <CurrentOrdersPanel />
) : activeTab === "buyHistory" ? (
  <BuyHistoryPanel />
) : (
  <SellHistoryPanel />
)}
```

Modify `frontend/invest/src/pages/mobile/MobilePortfolioPage.tsx`:

```ts
import { BuyHistoryPanel } from "../../components/my/BuyHistoryPanel";
```

Update the render branch:

```tsx
) : activeTab === "currentOrders" ? (
  <section style={{ padding: "0 16px" }}>
    <CurrentOrdersPanel compact />
  </section>
) : activeTab === "buyHistory" ? (
  <section style={{ padding: "0 16px" }}>
    <BuyHistoryPanel compact />
  </section>
) : (
  <section style={{ padding: "0 16px" }}>
    <SellHistoryPanel compact />
  </section>
)}
```

- [ ] **Step 6: Re-run buy-history tests**

Run:

```bash
npm --prefix frontend/invest test -- src/__tests__/BuyHistoryPanel.test.tsx src/__tests__/CurrentOrdersPanel.test.tsx
```

Expected: all tests pass.

- [ ] **Step 7: Commit Task 4**

```bash
git add frontend/invest/src/components/my/BuyHistoryPanel.tsx frontend/invest/src/__tests__/BuyHistoryPanel.test.tsx frontend/invest/src/components/my/portfolioTabs.ts frontend/invest/src/pages/desktop/DesktopPortfolioPage.tsx frontend/invest/src/pages/mobile/MobilePortfolioPage.tsx frontend/invest/src/__tests__/CurrentOrdersPanel.test.tsx
git commit -m "feat: add buy history portfolio tab"
```

### Task 5: Current Open-Order Name Enrichment And KIS KR Time Repair

**Files:**
- Modify: `app/routers/invest_open_orders.py`
- Modify: `app/services/current_orders_service.py`
- Test: `tests/test_current_orders_service.py`
- Test: `tests/routers/test_invest_open_orders_router.py`

- [ ] **Step 1: Add KIS KR `ord_tmd`-only test**

Append to `tests/test_current_orders_service.py` near existing normalize tests:

```python
def test_normalize_kis_kr_order_uses_today_kst_when_ord_dt_missing(monkeypatch) -> None:
    import app.services.current_orders_service as cos
    from app.services.current_orders_service import normalize_kis_order

    monkeypatch.setattr(
        cos,
        "now_kst",
        lambda: dt.datetime(2026, 6, 15, 12, 0, tzinfo=cos.KST),
    )

    row = normalize_kis_order(
        {
            "ord_no": "K1",
            "pdno": "005930",
            "sll_buy_dvsn_cd": "02",
            "ord_qty": "10",
            "ord_unpr": "70000",
            "ord_tmd": "090100",
        },
        market="kr",
        exchange="KRX",
    )

    assert row.ordered_at == dt.datetime(2026, 6, 15, 9, 1, tzinfo=cos.KST)
```

- [ ] **Step 2: Add enrichment tests**

Append to `tests/test_current_orders_service.py`:

```python
@pytest.mark.asyncio
async def test_current_orders_enriches_missing_toss_and_upbit_names(monkeypatch) -> None:
    from app.services import current_orders_service as cos
    from app.services.brokers.toss.dto import TossOrder, TossOrdersPage
    from app.services.current_orders_service import CurrentOrdersService

    async def fake_kr_names(symbols, db):
        assert symbols == ["005930"]
        assert db == "db-session"
        return {"005930": "삼성전자"}

    async def fake_us_names(symbols, db):
        assert symbols == ["AAPL"]
        assert db == "db-session"
        return {"AAPL": "Apple"}

    async def fake_crypto_names(markets, db):
        assert markets == ["KRW-BTC"]
        assert db == "db-session"
        return {"KRW-BTC": {"korean_name": "비트코인", "english_name": "Bitcoin"}}

    monkeypatch.setattr(cos, "get_kr_names_by_symbols", fake_kr_names)
    monkeypatch.setattr(cos, "get_us_names_by_symbols", fake_us_names)
    monkeypatch.setattr(cos, "get_upbit_market_display_names", fake_crypto_names)

    class _FakeToss:
        async def list_orders(self, **kwargs):
            return TossOrdersPage(
                orders=[
                    TossOrder(
                        order_id="T1",
                        symbol="005930",
                        side="BUY",
                        order_type="LIMIT",
                        time_in_force="DAY",
                        status="OPEN",
                        price=Decimal("70000"),
                        quantity=Decimal("1"),
                        order_amount=None,
                        currency="KRW",
                        ordered_at="2026-06-15T09:00:00+09:00",
                        canceled_at=None,
                        execution={"filledQuantity": Decimal("0")},
                    ),
                    TossOrder(
                        order_id="T2",
                        symbol="AAPL",
                        side="BUY",
                        order_type="LIMIT",
                        time_in_force="DAY",
                        status="OPEN",
                        price=Decimal("180"),
                        quantity=Decimal("1"),
                        order_amount=None,
                        currency="USD",
                        ordered_at="2026-06-15T09:00:00+09:00",
                        canceled_at=None,
                        execution={"filledQuantity": Decimal("0")},
                    ),
                ],
                next_cursor=None,
                has_next=False,
            )

        async def aclose(self) -> None:
            return None

    class _FakeUpbit:
        async def fetch_open_orders(self, market=None):
            return [
                {
                    "uuid": "UP1",
                    "market": "KRW-BTC",
                    "side": "bid",
                    "ord_type": "limit",
                    "price": "96000000",
                    "volume": "0.01",
                    "remaining_volume": "0.01",
                }
            ]

    service = CurrentOrdersService(
        kis_client_factory=None,
        upbit_client=_FakeUpbit(),
        toss_client_factory=lambda: _FakeToss(),
        db="db-session",  # type: ignore[arg-type]
        clock=lambda: dt.datetime(2026, 6, 15, 0, 0, tzinfo=dt.UTC),
    )

    response = await service.list_open_orders(market="all")

    names = {(row.broker, row.market, row.symbol): row.symbol_name for row in response.items}
    assert names[("toss", "kr", "005930")] == "삼성전자"
    assert names[("toss", "us", "AAPL")] == "Apple"
    assert names[("upbit", "crypto", "KRW-BTC")] == "비트코인"


@pytest.mark.asyncio
async def test_current_orders_name_lookup_failure_fails_open(monkeypatch) -> None:
    from app.services import current_orders_service as cos
    from app.services.current_orders_service import CurrentOrdersService

    async def boom(*args):
        raise RuntimeError("name lookup down")

    monkeypatch.setattr(cos, "get_kr_names_by_symbols", boom)

    class _FakeKIS:
        async def inquire_korea_orders(self, is_mock: bool = False):
            return [{"ord_no": "K1", "pdno": "005930", "ord_qty": "1", "ord_unpr": "70000"}]

        async def inquire_overseas_orders(self, exchange_code: str = "NASD", is_mock: bool = False):
            return []

    service = CurrentOrdersService(
        kis_client_factory=lambda: _FakeKIS(),
        upbit_client=None,
        toss_client_factory=None,
        db="db-session",  # type: ignore[arg-type]
        clock=lambda: dt.datetime(2026, 6, 15, 0, 0, tzinfo=dt.UTC),
    )

    response = await service.list_open_orders(market="kr")

    assert response.count == 1
    assert response.items[0].symbol == "005930"
    assert response.items[0].symbol_name is None
```

- [ ] **Step 3: Add router dependency smoke test**

In `tests/routers/test_invest_open_orders_router.py`, import `AsyncMock`:

```python
from unittest.mock import AsyncMock
```

Add:

```python
@pytest.mark.unit
def test_open_orders_default_service_receives_db_dependency(monkeypatch) -> None:
    from app.core.db import get_db
    from app.routers import invest_open_orders
    from app.routers.dependencies import get_authenticated_user

    captured = {}

    class _Service:
        def __init__(self, *, db):
            captured["db"] = db

        async def list_open_orders(self, *, market: str = "all") -> OpenOrdersResponse:
            return OpenOrdersResponse(
                market=market,  # type: ignore[arg-type]
                count=0,
                data_state="ok",
                as_of=dt.datetime(2026, 6, 15, 0, 0, tzinfo=dt.UTC),
                items=[],
                sources=[],
                warnings=[],
                empty_reason="no open orders for the selected market",
            )

    monkeypatch.setattr(invest_open_orders, "CurrentOrdersService", _Service)

    app = FastAPI()
    app.include_router(invest_open_orders.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_db] = lambda: "db-session"

    response = TestClient(app).get("/trading/api/invest/open-orders")

    assert response.status_code == 200
    assert captured["db"] == "db-session"
```

- [ ] **Step 4: Run backend current-order tests and confirm failure**

Run:

```bash
uv run pytest tests/test_current_orders_service.py::test_normalize_kis_kr_order_uses_today_kst_when_ord_dt_missing tests/test_current_orders_service.py::test_current_orders_enriches_missing_toss_and_upbit_names tests/test_current_orders_service.py::test_current_orders_name_lookup_failure_fails_open tests/routers/test_invest_open_orders_router.py::test_open_orders_default_service_receives_db_dependency -q
```

Expected: KIS time test fails because `ord_dt` is required; enrichment tests fail because the service has no DB/name lookup wiring; router dependency test fails because `get_current_orders_service` does not receive DB.

- [ ] **Step 5: Inject DB in the open-orders router**

Modify `app/routers/invest_open_orders.py`:

```python
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.open_orders import OpenOrdersResponse
from app.services.current_orders_service import CurrentOrdersService
```

Replace service dependency:

```python
def get_current_orders_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CurrentOrdersService:
    return CurrentOrdersService(db=db)
```

- [ ] **Step 6: Add KST parsing and enrichment implementation**

Modify imports in `app/services/current_orders_service.py`:

```python
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import KST, now_kst
from app.services.kr_symbol_universe_service import get_kr_names_by_symbols
from app.services.upbit_symbol_universe_service import get_upbit_market_display_names
from app.services.us_symbol_universe_service import get_us_names_by_symbols
```

Remove:

```python
_KST = dt.timezone(dt.timedelta(hours=9), name="KST")
```

Replace `_parse_kis_ordered_at`:

```python
def _parse_kis_ordered_at(row: dict[str, Any]) -> dt.datetime | None:
    explicit = _parse_datetime(row.get("ordered_at") or row.get("placed_at"))
    if explicit is not None:
        return explicit
    ord_tmd = str(row.get("ord_tmd") or "").strip()
    if not ord_tmd:
        return None
    ord_dt = str(row.get("ord_dt") or "").strip() or now_kst().strftime("%Y%m%d")
    try:
        return dt.datetime.strptime(f"{ord_dt}{ord_tmd.zfill(6)}", "%Y%m%d%H%M%S").replace(
            tzinfo=KST
        )
    except ValueError:
        return None
```

Update `CurrentOrdersService.__init__`:

```python
        toss_client_factory: Callable[[], Any] | None = _default_toss_client,
        db: AsyncSession | None = None,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._kis_client_factory = kis_client_factory
        self._kis_client_initialized = False
        self._kis_client: _KISClientProtocol | None = None
        self._upbit_client = upbit_client
        self._toss_client_factory = toss_client_factory
        self._db = db
        self._clock = clock or (lambda: dt.datetime.now(tz=dt.UTC))
```

Add method inside `CurrentOrdersService` before `list_open_orders`:

```python
    async def _attach_symbol_names(self, rows: list[OpenOrderRow]) -> list[OpenOrderRow]:
        """Best-effort display-name enrichment for broker rows that lack names."""
        if self._db is None or not rows:
            return rows

        kr_symbols = sorted({row.symbol for row in rows if row.market == "kr" and not row.symbol_name})
        us_symbols = sorted({row.symbol for row in rows if row.market == "us" and not row.symbol_name})
        crypto_markets = sorted({row.symbol.strip().upper() for row in rows if row.market == "crypto" and not row.symbol_name})

        async def _safe(coro, label: str):
            try:
                return await coro
            except Exception:  # noqa: BLE001 - display names must fail open
                logger.warning("open-order symbol-name resolution failed for %s", label, exc_info=True)
                return {}

        kr_names = (
            await _safe(get_kr_names_by_symbols(kr_symbols, self._db), "kr")
            if kr_symbols
            else {}
        )
        us_names = (
            await _safe(get_us_names_by_symbols(us_symbols, self._db), "us")
            if us_symbols
            else {}
        )
        crypto_names = (
            await _safe(get_upbit_market_display_names(crypto_markets, self._db), "crypto")
            if crypto_markets
            else {}
        )

        enriched: list[OpenOrderRow] = []
        for row in rows:
            if row.symbol_name:
                enriched.append(row)
                continue
            name: str | None = None
            if row.market == "kr":
                name = kr_names.get(row.symbol)
            elif row.market == "us":
                name = us_names.get(row.symbol)
            elif row.market == "crypto":
                display = crypto_names.get(row.symbol.strip().upper())
                if display:
                    name = display.get("korean_name") or display.get("english_name")
            if name and name != row.symbol:
                enriched.append(row.model_copy(update={"symbol_name": name}))
            else:
                enriched.append(row)
        return enriched
```

In `list_open_orders`, after sorting rows:

```python
        rows.sort(key=_sort_key, reverse=True)
        rows = await self._attach_symbol_names(rows)
        data_state = _overall_state(sources)
```

- [ ] **Step 7: Re-run current-order backend tests**

Run:

```bash
uv run pytest tests/test_current_orders_service.py::test_normalize_kis_kr_order_uses_today_kst_when_ord_dt_missing tests/test_current_orders_service.py::test_current_orders_enriches_missing_toss_and_upbit_names tests/test_current_orders_service.py::test_current_orders_name_lookup_failure_fails_open tests/routers/test_invest_open_orders_router.py::test_open_orders_default_service_receives_db_dependency -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit Task 5**

```bash
git add app/routers/invest_open_orders.py app/services/current_orders_service.py tests/test_current_orders_service.py tests/routers/test_invest_open_orders_router.py
git commit -m "fix: enrich current order names and repair kis kr times"
```

### Task 6: Full Verification And Clean-Up

**Files:**
- Verify all modified files.

- [ ] **Step 1: Run focused backend test suite**

Run:

```bash
uv run pytest tests/test_current_orders_service.py tests/routers/test_invest_open_orders_router.py tests/routers/test_invest_fills_router.py -q
```

Expected: all selected backend tests pass.

- [ ] **Step 2: Run focused frontend tests**

Run:

```bash
npm --prefix frontend/invest test -- src/__tests__/fills.api.test.ts src/__tests__/stockDetailPath.test.ts src/__tests__/CurrentOrdersPanel.test.tsx src/__tests__/SellHistoryPanel.test.tsx src/__tests__/BuyHistoryPanel.test.tsx
```

Expected: all selected frontend tests pass.

- [ ] **Step 3: Run frontend typecheck**

Run:

```bash
npm --prefix frontend/invest run typecheck
```

Expected: TypeScript exits 0.

- [ ] **Step 4: Run project lint**

Run:

```bash
make lint
```

Expected: Ruff and configured lint checks exit 0.

- [ ] **Step 5: Inspect diff for accidental scope creep**

Run:

```bash
git diff --stat HEAD
git diff --check
```

Expected: diff only touches the files listed in this plan; `git diff --check` reports no whitespace errors.

- [ ] **Step 6: Final commit if verification required changes**

If Step 1-5 required fixes after previous commits:

```bash
git add app frontend tests
git commit -m "test: verify rob 577 follow-up"
```

If no fixes were needed, do not create an empty commit.

## Self-Review

- Spec coverage:
  - ASK 1 symbol click to detail: Task 3 covers `stockDetailPath`, current orders, sell fills, and buy fills.
  - ASK 2 buy fills: Task 1, Task 2, and Task 4 cover `/recent?side=buy` and the standalone `매수 이력` tab.
  - Toss/Upbit names: Task 5 covers session-backed enrichment.
  - KIS KR ordered time: Task 5 covers `ord_tmd`-only date synthesis with KST.
  - Market-order price label: Task 3 covers current-order `price=null` display.
  - No migration: all tasks are code/test only.
- Placeholder scan:
  - No deferred implementation notes.
  - Follow-up consolidation into a unified fills view is intentionally outside this plan and not required for ROB-577.
- Type consistency:
  - Backend side type uses existing `app.schemas.execution_ledger.Side`.
  - Frontend side type uses existing `FillSide`.
  - `stockDetailPath` accepts the already-defined `RouteMarket` shape.

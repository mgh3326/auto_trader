# Crypto Stock Detail Bare Symbol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix `/invest/stocks/crypto/BTC` so stock detail accepts and canonicalizes bare Upbit base symbols like `BTC` to `KRW-BTC` instead of returning `/invest/api/stock-detail/crypto/BTC 404`.

**Architecture:** Make the backend stock-detail resolver tolerant of all crypto route forms (`BTC`, `btc`, `BTC-KRW`, `KRW-BTC`) by normalizing bare base symbols to the Upbit KRW market code before querying `upbit_symbol_universe.market`. Also canonicalize frontend crypto stock-detail URLs to `KRW-*` so right-panel clicks and recents produce stable paths, while backend support keeps old bookmarks working.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, pytest, React 19, React Router, TypeScript, Vitest.

---

## Files

- Modify: `app/services/invest_view_model/stock_detail_symbol_resolver.py`
  - Responsibility: Normalize crypto route symbols before DB lookup.
- Modify: `tests/test_stock_detail_symbol_resolver.py`
  - Responsibility: Prove `BTC` and other route variants query `upbit_symbol_universe.market = 'KRW-BTC'`.
- Modify: `frontend/invest/src/stockDetailPath.ts`
  - Responsibility: Build canonical frontend detail paths for crypto holdings and expose a shared route-symbol normalizer.
- Modify: `frontend/invest/src/desktop/RightRemotePanel.tsx`
  - Responsibility: Use the shared route-symbol normalizer for recent/watch/realtime fallback routes.
- Create: `frontend/invest/src/__tests__/stockDetailPath.test.ts`
  - Responsibility: Unit-test route canonicalization for crypto and preservation for KR/US.
- Modify: `frontend/invest/src/__tests__/RightRemotePanel.test.tsx`
  - Responsibility: Regression-test actual Upbit-style `symbol: "BTC"` holding navigation.

No DB migration, router registration, auth change, or live-order behavior is required.

---

### Task 1: Backend Resolver Accepts Bare Crypto Symbols

**Files:**
- Modify: `tests/test_stock_detail_symbol_resolver.py`
- Modify: `app/services/invest_view_model/stock_detail_symbol_resolver.py`

- [ ] **Step 1: Write the failing resolver test**

Replace the current `test_resolve_symbol_crypto_uses_upbit_market` in `tests/test_stock_detail_symbol_resolver.py` with this parametrized version:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_symbol", "expected_lookup"),
    [
        ("BTC", "KRW-BTC"),
        ("btc", "KRW-BTC"),
        ("BTC-KRW", "KRW-BTC"),
        ("KRW-BTC", "KRW-BTC"),
    ],
)
async def test_resolve_symbol_crypto_normalizes_route_inputs_to_upbit_market(
    raw_symbol: str, expected_lookup: str
):
    db = FakeSession(
        SimpleNamespace(
            market="KRW-BTC",
            base_currency="BTC",
            quote_currency="KRW",
            korean_name="비트코인",
            english_name="Bitcoin",
            is_active=True,
        )
    )

    resolved = await resolve_symbol("crypto", raw_symbol, db)

    compiled = str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert f"upbit_symbol_universe.market = '{expected_lookup}'" in compiled
    assert resolved.symbol_db == "KRW-BTC"
    assert resolved.display_name == "비트코인"
    assert resolved.exchange == "KRW"
    assert resolved.asset_type == "crypto"
```

- [ ] **Step 2: Run the backend test and verify it fails**

Run:

```bash
uv run pytest tests/test_stock_detail_symbol_resolver.py::test_resolve_symbol_crypto_normalizes_route_inputs_to_upbit_market -q
```

Expected: FAIL for the `BTC` and `btc` cases. The compiled SQL will contain:

```text
upbit_symbol_universe.market = 'BTC'
```

instead of:

```text
upbit_symbol_universe.market = 'KRW-BTC'
```

- [ ] **Step 3: Implement minimal backend normalization**

In `app/services/invest_view_model/stock_detail_symbol_resolver.py`, replace `_normalize_crypto_market` with:

```python
def _normalize_crypto_market(raw_symbol: str) -> str:
    symbol = raw_symbol.strip().upper()
    if symbol.startswith("KRW-"):
        return symbol
    if symbol.endswith("-KRW"):
        base = symbol.removesuffix("-KRW")
        return f"KRW-{base}"
    if "-" not in symbol:
        return f"KRW-{symbol}"
    return symbol
```

This keeps non-KRW hyphenated symbols unchanged, but maps bare `BTC`/`ETH` style route symbols to the KRW Upbit universe key.

- [ ] **Step 4: Run backend resolver tests**

Run:

```bash
uv run pytest tests/test_stock_detail_symbol_resolver.py -q
```

Expected: PASS.

- [ ] **Step 5: Run adjacent backend stock-detail tests**

Run:

```bash
uv run pytest tests/test_stock_detail_symbol_resolver.py tests/test_stock_detail_candles.py tests/test_stock_detail_orders.py tests/test_stock_detail_service.py -q
```

Expected: PASS. This confirms shared `_normalize_crypto_market` behavior did not break candles/orders/detail view-model behavior.

---

### Task 2: Frontend Builds Canonical Crypto Detail URLs

**Files:**
- Modify: `frontend/invest/src/stockDetailPath.ts`
- Modify: `frontend/invest/src/desktop/RightRemotePanel.tsx`
- Create: `frontend/invest/src/__tests__/stockDetailPath.test.ts`
- Modify: `frontend/invest/src/__tests__/RightRemotePanel.test.tsx`

- [ ] **Step 1: Write stock detail path tests**

Create `frontend/invest/src/__tests__/stockDetailPath.test.ts`:

```ts
import { expect, test } from "vitest";
import { stockDetailPath, stockDetailRouteSymbol } from "../stockDetailPath";

test("stock detail path canonicalizes bare crypto symbols to KRW market codes", () => {
  expect(stockDetailPath("CRYPTO", "BTC")).toBe("/stocks/crypto/KRW-BTC");
  expect(stockDetailPath("CRYPTO", "btc")).toBe("/stocks/crypto/KRW-BTC");
  expect(stockDetailPath("CRYPTO", "BTC-KRW")).toBe("/stocks/crypto/KRW-BTC");
  expect(stockDetailPath("CRYPTO", "KRW-BTC")).toBe("/stocks/crypto/KRW-BTC");
});

test("stock detail path preserves KR and US symbols", () => {
  expect(stockDetailPath("KR", "005930")).toBe("/stocks/kr/005930");
  expect(stockDetailPath("US", "BRK-B")).toBe("/stocks/us/BRK-B");
});

test("stock detail route symbol supports lowercase route market keys for recent symbols", () => {
  expect(stockDetailRouteSymbol("crypto", "BTC")).toBe("KRW-BTC");
  expect(stockDetailRouteSymbol("crypto", "btc-krw")).toBe("KRW-BTC");
  expect(stockDetailRouteSymbol("us", "BRK-B")).toBe("BRK-B");
});
```

- [ ] **Step 2: Run the new frontend test and verify it fails**

Run:

```bash
cd frontend/invest
npm test -- src/__tests__/stockDetailPath.test.ts
```

Expected: FAIL because `stockDetailRouteSymbol` does not exist and `stockDetailPath("CRYPTO", "BTC")` currently returns `/stocks/crypto/BTC`.

- [ ] **Step 3: Implement shared frontend route normalization**

Replace `frontend/invest/src/stockDetailPath.ts` with:

```ts
import type { Market } from "./types/invest";

type RouteMarket = Market | "kr" | "us" | "crypto";
type StockDetailMarketParam = "kr" | "us" | "crypto";

const MARKET_ROUTE: Record<Market, StockDetailMarketParam> = {
  KR: "kr",
  US: "us",
  CRYPTO: "crypto",
};

function routeMarketParam(market: RouteMarket): StockDetailMarketParam {
  if (market === "KR" || market === "US" || market === "CRYPTO") {
    return MARKET_ROUTE[market];
  }
  return market;
}

function normalizeCryptoRouteSymbol(symbol: string): string {
  const clean = symbol.trim().toUpperCase();
  if (!clean) return clean;
  if (clean.startsWith("KRW-")) return clean;
  if (clean.endsWith("-KRW")) return `KRW-${clean.slice(0, -4)}`;
  if (!clean.includes("-")) return `KRW-${clean}`;
  return clean;
}

export function stockDetailMarketParam(market: Market): StockDetailMarketParam {
  return MARKET_ROUTE[market];
}

export function stockDetailRouteSymbol(market: RouteMarket, symbol: string): string {
  const cleanSymbol = symbol.trim();
  if (routeMarketParam(market) !== "crypto") return cleanSymbol;
  return normalizeCryptoRouteSymbol(cleanSymbol);
}

export function stockDetailPath(market: Market, symbol: string): string | null {
  const cleanSymbol = symbol.trim();
  if (!cleanSymbol) return null;
  const marketParam = MARKET_ROUTE[market];
  return `/stocks/${marketParam}/${encodeURIComponent(stockDetailRouteSymbol(market, cleanSymbol))}`;
}
```

- [ ] **Step 4: Update right panel fallback routes to use the shared normalizer**

In `frontend/invest/src/desktop/RightRemotePanel.tsx`, change the import:

```ts
import { stockDetailPath, stockDetailRouteSymbol } from "../stockDetailPath";
```

Then replace `stockRouteForMarketKey` with:

```ts
function stockRouteForMarketKey(market: MarketKey, symbol: string): string {
  return `/stocks/${market}/${encodeURIComponent(stockDetailRouteSymbol(market, symbol))}`;
}
```

This covers recent symbols, watchlist symbols, and realtime fallback routes in addition to portfolio holdings.

- [ ] **Step 5: Add right-panel regression test for actual Upbit-style bare symbol**

Append this test to `frontend/invest/src/__tests__/RightRemotePanel.test.tsx` near the existing navigation test:

```tsx
test("crypto portfolio clicks canonicalize bare Upbit base symbols to KRW detail routes", async () => {
  const user = userEvent.setup();
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue({
    ...PANEL_RESP,
    groupedHoldings: PANEL_RESP.groupedHoldings.map((holding) =>
      holding.market === "CRYPTO"
        ? {
            ...holding,
            groupId: "CRYPTO:crypto:KRW:BTC",
            symbol: "BTC",
            displayName: "비트코인",
          }
        : holding,
    ),
  });

  renderPanel();
  await waitFor(() => expect(screen.getByTestId("portfolio-panel")).toBeInTheDocument());

  await user.click(screen.getByRole("button", { name: /비트코인/ }));

  expect(screen.getByTestId("location-probe")).toHaveAttribute("data-path", "/stocks/crypto/KRW-BTC");
});
```

- [ ] **Step 6: Run focused frontend tests**

Run:

```bash
cd frontend/invest
npm test -- src/__tests__/stockDetailPath.test.ts src/__tests__/RightRemotePanel.test.tsx
```

Expected: PASS.

---

### Task 3: Final Verification

**Files:**
- No additional source files.

- [ ] **Step 1: Run focused backend tests**

Run:

```bash
uv run pytest tests/test_stock_detail_symbol_resolver.py tests/test_stock_detail_candles.py tests/test_stock_detail_orders.py tests/test_stock_detail_service.py -q
```

Expected: PASS.

- [ ] **Step 2: Run focused frontend tests**

Run:

```bash
cd frontend/invest
npm test -- src/__tests__/stockDetailPath.test.ts src/__tests__/RightRemotePanel.test.tsx src/__tests__/StockDetailPage.test.tsx src/__tests__/stockDetailApi.test.ts
```

Expected: PASS.

- [ ] **Step 3: Optional manual verification against local app**

Start the app using the repo's normal dev command:

```bash
make dev
```

Open:

```text
http://localhost:8000/invest/stocks/crypto/BTC
```

Expected: the stock detail page renders with response `symbol: KRW-BTC`; the above-fold shell does not show `/invest/api/stock-detail/crypto/BTC 404`.

- [ ] **Step 4: Review diff scope**

Run:

```bash
git diff -- app/services/invest_view_model/stock_detail_symbol_resolver.py tests/test_stock_detail_symbol_resolver.py frontend/invest/src/stockDetailPath.ts frontend/invest/src/desktop/RightRemotePanel.tsx frontend/invest/src/__tests__/stockDetailPath.test.ts frontend/invest/src/__tests__/RightRemotePanel.test.tsx
```

Expected: diff only contains crypto symbol normalization, route canonicalization, and tests. No auth, trading, DB schema, broker, or execution behavior changes.


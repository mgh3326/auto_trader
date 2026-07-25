// ROB-559 — fetchStockDetailOrderLedger normalizes snake_case rows to LinkedOrder.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchStockDetailOrderLedger } from "../api/stockDetail";

const originalFetch = global.fetch;

afterEach(() => {
  global.fetch = originalFetch;
});

describe("fetchStockDetailOrderLedger (ROB-559)", () => {
  it("maps snake_case order-ledger rows to camelCase LinkedOrder", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        count: 1,
        items: [
          {
            ledger_id: 11,
            broker: "upbit",
            account_scope: "upbit_live",
            market: "crypto",
            order_no: "7aeb17dd-2fa2",
            symbol: "KRW-BTC",
            side: "buy",
            status: "filled",
            filled_qty: "0.01",
            avg_fill_price: "96180000",
          },
        ],
      }),
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    const rows = await fetchStockDetailOrderLedger({
      market: "crypto",
      symbol: "KRW-BTC",
      days: 90,
    });

    expect(rows).toHaveLength(1);
    expect(rows[0]?.ledgerId).toBe(11);
    expect(rows[0]?.orderNo).toBe("7aeb17dd-2fa2");
    expect(rows[0]?.accountScope).toBe("upbit_live");
    expect(rows[0]?.market).toBe("crypto");
    expect(rows[0]?.filledQty).toBe("0.01");
    // crypto sends the raw pair as-is in the request path
    const calledUrl = String(fetchMock.mock.calls[0]?.[0]);
    expect(calledUrl).toContain("/stock-detail/crypto/KRW-BTC/order-ledger");
    expect(calledUrl).toContain("days=90");
  });

  it("returns [] when the backend sends no items", async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ count: 0, items: [] }),
    }) as unknown as typeof fetch;

    const rows = await fetchStockDetailOrderLedger({ market: "us", symbol: "AAPL" });
    expect(rows).toEqual([]);
  });
});

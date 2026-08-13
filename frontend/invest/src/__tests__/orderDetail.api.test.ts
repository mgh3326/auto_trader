import { afterEach, describe, expect, it, vi } from "vitest";

import {
  fetchOrderDetail,
  OrderDetailNotFoundError,
  OrderDetailUnknownLedgerError,
} from "../api/orderDetail";

const originalFetch = global.fetch;

afterEach(() => {
  global.fetch = originalFetch;
});

describe("fetchOrderDetail", () => {
  it("calls order-detail with broker + market + ledger_id and normalizes the response", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        broker: "kis",
        account_scope: "kis_live",
        market: "kr",
        order_no: "0001234500",
        ledger_id: 42,
        symbol: "005930",
        side: "buy",
        status: "filled",
        filled_qty: "10",
        avg_fill_price: "70000",
        order_time: "093015",
        reconciled_at: "2026-05-10T09:05:00Z",
        exit_reason: null,
        thesis: "저점 매수",
        report_item_uuid: null,
      }),
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    const order = await fetchOrderDetail("kis", "kr", 42);

    expect(fetchMock).toHaveBeenCalledWith(
      "/trading/api/invest/fills/order-detail?broker=kis&market=kr&ledger_id=42",
      { credentials: "include" },
    );
    expect(order.ledgerId).toBe(42);
    expect(order.thesis).toBe("저점 매수");
  });

  // verify-r1 BLOCKER-1 regression: "kis" is written to two different ledger
  // tables (KR domestic vs. US live via KIS) with independent id sequences —
  // market must travel alongside broker or the two collide on ledger_id.
  it("passes market as a distinct query param from broker (US vs KR disambiguation)", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        broker: "kis",
        market: "us",
        ledger_id: 42,
        symbol: "AAPL",
        side: "sell",
        status: "filled",
        thesis: "US 밸류에이션 부담으로 축소",
      }),
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    const order = await fetchOrderDetail("kis", "us", 42);

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe("/trading/api/invest/fills/order-detail?broker=kis&market=us&ledger_id=42");
    expect(order.symbol).toBe("AAPL");
    expect(order.thesis).toBe("US 밸류에이션 부담으로 축소");
  });

  it("throws OrderDetailNotFoundError on 404", async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({ ok: false, status: 404 }) as unknown as typeof fetch;

    await expect(fetchOrderDetail("kis", "kr", 999)).rejects.toBeInstanceOf(OrderDetailNotFoundError);
  });

  it("throws OrderDetailUnknownLedgerError on 400 (unrecognized broker+market combo)", async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({ ok: false, status: 400 }) as unknown as typeof fetch;

    await expect(fetchOrderDetail("upbit", "kr", 1)).rejects.toBeInstanceOf(
      OrderDetailUnknownLedgerError,
    );
  });

  it("throws a generic error on other non-ok responses", async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({ ok: false, status: 500 }) as unknown as typeof fetch;

    await expect(fetchOrderDetail("kis", "kr", 1)).rejects.toThrow("order-detail 500");
  });
});

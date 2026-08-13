import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchOrderDetail, OrderDetailNotFoundError } from "../api/orderDetail";

const originalFetch = global.fetch;

afterEach(() => {
  global.fetch = originalFetch;
});

describe("fetchOrderDetail", () => {
  it("calls order-detail with broker + ledger_id and normalizes the response", async () => {
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

    const order = await fetchOrderDetail("kis", 42);

    expect(fetchMock).toHaveBeenCalledWith(
      "/trading/api/invest/fills/order-detail?broker=kis&ledger_id=42",
      { credentials: "include" },
    );
    expect(order.ledgerId).toBe(42);
    expect(order.thesis).toBe("저점 매수");
  });

  it("throws OrderDetailNotFoundError on 404", async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({ ok: false, status: 404 }) as unknown as typeof fetch;

    await expect(fetchOrderDetail("kis", 999)).rejects.toBeInstanceOf(OrderDetailNotFoundError);
  });

  it("throws a generic error on other non-ok responses", async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({ ok: false, status: 500 }) as unknown as typeof fetch;

    await expect(fetchOrderDetail("kis", 1)).rejects.toThrow("order-detail 500");
  });
});

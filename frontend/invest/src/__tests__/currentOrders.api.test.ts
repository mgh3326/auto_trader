import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchCurrentOrders } from "../api/currentOrders";

const originalFetch = global.fetch;

afterEach(() => {
  global.fetch = originalFetch;
});

describe("fetchCurrentOrders", () => {
  it("calls the open-orders endpoint with credentials and default market", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ market: "all", count: 0, items: [], sources: [], warnings: [] }),
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    await fetchCurrentOrders();

    expect(fetchMock).toHaveBeenCalledWith(
      "/trading/api/invest/open-orders?market=all",
      { credentials: "include" },
    );
  });

  it("passes the selected market", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ market: "crypto", count: 0, items: [], sources: [], warnings: [] }),
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    await fetchCurrentOrders("crypto");

    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("market=crypto");
  });

  it("throws on non-ok responses", async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: false,
      status: 500,
    }) as unknown as typeof fetch;

    await expect(fetchCurrentOrders("kr")).rejects.toThrow("open-orders 500");
  });
});

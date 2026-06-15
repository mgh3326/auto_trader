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

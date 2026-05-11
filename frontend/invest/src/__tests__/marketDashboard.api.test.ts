import { afterEach, expect, test, vi } from "vitest";

import { fetchMarketDashboard } from "../api/marketDashboard";

const PAYLOAD = {
  asOf: "2026-05-11T05:00:00Z",
  state: "fresh",
  sections: [],
  warnings: [],
  notes: [],
};

afterEach(() => {
  vi.restoreAllMocks();
});

test("fetchMarketDashboard reads the read-only invest market endpoint", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(PAYLOAD), { status: 200, headers: { "content-type": "application/json" } }),
  );

  await expect(fetchMarketDashboard()).resolves.toEqual(PAYLOAD);
  expect(fetchMock).toHaveBeenCalledWith("/invest/api/market", {
    credentials: "include",
    signal: undefined,
  });
});

test("fetchMarketDashboard raises a scrubbed endpoint/status error", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("nope", { status: 503 }));

  await expect(fetchMarketDashboard()).rejects.toThrow("/invest/api/market 503");
});

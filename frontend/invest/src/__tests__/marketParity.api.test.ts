import { afterEach, expect, test, vi } from "vitest";

import { fetchMarketParity } from "../api/marketParity";

const PAYLOAD = {
  market: "kr",
  state: "partial",
  asOf: "2026-05-14T00:00:00Z",
  cards: [],
  warnings: [],
  notes: [],
};

afterEach(() => {
  vi.restoreAllMocks();
});

test("fetchMarketParity reads the read-only dashed market parity endpoint", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(PAYLOAD), { status: 200, headers: { "content-type": "application/json" } }),
  );

  await expect(fetchMarketParity()).resolves.toEqual(PAYLOAD);
  expect(fetchMock).toHaveBeenCalledWith("/invest/api/market-parity?market=kr&includeDisabled=true&limit=8", {
    credentials: "include",
    signal: undefined,
  });
});

test("fetchMarketParity raises a scrubbed endpoint/status error", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("nope", { status: 503 }));

  await expect(fetchMarketParity()).rejects.toThrow("/invest/api/market-parity 503");
});

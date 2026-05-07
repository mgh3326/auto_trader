import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { fetchMarketEventsToday } from "../api/marketEvents";
import type { MarketEventsDayResponse } from "../types/marketEvents";

const baseResponse: MarketEventsDayResponse = {
  date: "2026-05-13",
  events: [],
};

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("fetchMarketEventsToday hits the today endpoint with credentials", async () => {
  fetchMock.mockResolvedValueOnce({ ok: true, json: async () => baseResponse });

  const result = await fetchMarketEventsToday();

  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(url).toBe("/trading/api/market-events/today");
  expect(init).toMatchObject({ credentials: "include" });
  expect(result).toEqual(baseResponse);
});

test("fetchMarketEventsToday forwards category/market filters", async () => {
  fetchMock.mockResolvedValueOnce({ ok: true, json: async () => baseResponse });

  await fetchMarketEventsToday({ category: "economic", market: "global" });

  const [url] = fetchMock.mock.calls[0] as [string];
  expect(url).toBe(
    "/trading/api/market-events/today?category=economic&market=global",
  );
});

test("fetchMarketEventsToday throws on non-ok response", async () => {
  fetchMock.mockResolvedValueOnce({
    ok: false,
    status: 500,
    json: async () => ({}),
  });
  await expect(fetchMarketEventsToday()).rejects.toThrow(/500/);
});

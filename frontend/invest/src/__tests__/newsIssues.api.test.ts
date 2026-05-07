// frontend/invest/src/__tests__/newsIssues.api.test.ts
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { fetchNewsIssues } from "../api/newsIssues";
import type { MarketIssuesResponse } from "../types/newsIssues";

const baseResponse: MarketIssuesResponse = {
  market: "all",
  as_of: "2026-05-07T00:00:00Z",
  window_hours: 24,
  items: [],
};

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("fetchNewsIssues uses default query params and credentials", async () => {
  fetchMock.mockResolvedValueOnce({ ok: true, json: async () => baseResponse });

  await fetchNewsIssues();

  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(url).toBe(
    "/trading/api/news-issues?market=all&window_hours=24&limit=20",
  );
  expect(init).toMatchObject({ credentials: "include" });
});

test("fetchNewsIssues throws on non-ok response", async () => {
  fetchMock.mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({}) });
  await expect(fetchNewsIssues()).rejects.toThrow(/503/);
});

test("fetchNewsIssues overrides params", async () => {
  fetchMock.mockResolvedValueOnce({ ok: true, json: async () => baseResponse });
  await fetchNewsIssues({ market: "kr", windowHours: 12, limit: 5 });
  const [url] = fetchMock.mock.calls[0] as [string];
  expect(url).toBe(
    "/trading/api/news-issues?market=kr&window_hours=12&limit=5",
  );
});

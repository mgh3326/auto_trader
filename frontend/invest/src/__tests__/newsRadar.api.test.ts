// frontend/invest/src/__tests__/newsRadar.api.test.ts
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { fetchNewsRadar } from "../api/newsRadar";
import type { NewsRadarResponse } from "../types/newsRadar";

const baseResponse: NewsRadarResponse = {
  market: "all",
  as_of: "2026-05-07T00:00:00Z",
  readiness: {
    status: "ready",
    latest_scraped_at: null,
    latest_published_at: null,
    recent_6h_count: 0,
    recent_24h_count: 0,
    source_count: 0,
    stale: false,
    max_age_minutes: 0,
    warnings: [],
  },
  summary: {
    high_risk_count: 0,
    total_count: 0,
    included_in_briefing_count: 0,
    excluded_but_collected_count: 0,
  },
  sections: [],
  items: [],
  excluded_items: [],
  source_coverage: [],
};

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("fetchNewsRadar uses default query params and credentials", async () => {
  fetchMock.mockResolvedValueOnce({ ok: true, json: async () => baseResponse });

  await fetchNewsRadar();

  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0];
  expect(url).toBe(
    "/trading/api/news-radar?market=all&hours=24&include_excluded=true&limit=20",
  );
  expect(init).toMatchObject({ credentials: "include" });
});

test("fetchNewsRadar throws on non-ok response", async () => {
  fetchMock.mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({}) });
  await expect(fetchNewsRadar()).rejects.toThrow(/503/);
});

test("fetchNewsRadar overrides params", async () => {
  fetchMock.mockResolvedValueOnce({ ok: true, json: async () => baseResponse });
  await fetchNewsRadar({ market: "kr", hours: 12, limit: 5, includeExcluded: false });
  const [url] = fetchMock.mock.calls[0];
  expect(url).toBe(
    "/trading/api/news-radar?market=kr&hours=12&include_excluded=false&limit=5",
  );
});

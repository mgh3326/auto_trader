import { afterEach, expect, test, vi } from "vitest";
import { fetchFeedResearch } from "../api/feedResearch";
import type { FeedResearchResponse } from "../types/feedResearch";

const response: FeedResearchResponse = {
  tab: "latest",
  asOf: "2026-05-10T00:00:00Z",
  items: [],
  nextCursor: null,
  meta: { limit: 30, appliedFilters: {} },
};

afterEach(() => {
  vi.unstubAllGlobals();
});

test("calls feed research API with credentials and camel-case filters", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => response });
  vi.stubGlobal("fetch", fetchMock);

  await expect(
    fetchFeedResearch({
      tab: "latest",
      limit: 30,
      cursor: "cursor-1",
      source: "kis_research",
      symbol: "005930",
      analyst: "홍길동",
      category: "기업분석",
      query: "반도체",
      fromDate: "2026-05-01",
      toDate: "2026-05-10",
    }),
  ).resolves.toEqual(response);

  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0]!;
  expect(init).toEqual({ credentials: "include" });
  expect(String(url)).toMatch(/^\/invest\/api\/feed\/research\?/);
  const params = new URLSearchParams(String(url).split("?")[1]);
  expect(params.get("tab")).toBe("latest");
  expect(params.get("limit")).toBe("30");
  expect(params.get("cursor")).toBe("cursor-1");
  expect(params.get("source")).toBe("kis_research");
  expect(params.get("symbol")).toBe("005930");
  expect(params.get("analyst")).toBe("홍길동");
  expect(params.get("category")).toBe("기업분석");
  expect(params.get("query")).toBe("반도체");
  expect(params.get("fromDate")).toBe("2026-05-01");
  expect(params.get("toDate")).toBe("2026-05-10");
  expect(params.has("from_date")).toBe(false);
  expect(params.has("to_date")).toBe(false);
});

test("rejects non-OK responses with endpoint and status", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 401 }));

  await expect(fetchFeedResearch({ tab: "latest" })).rejects.toThrow("feed/research 401");
});

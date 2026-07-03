import { afterEach, expect, test, vi } from "vitest";
import { fetchOpenNextActions, fetchRetrospectives } from "../api/retrospectives";
import type { NextActionsResponse, RetrospectivesResponse } from "../types/retrospectives";

const listResponse: RetrospectivesResponse = {
  market: "kr", trigger_type: null, root_cause_class: null, symbol: null,
  count: 0, total: 0, items: [], as_of: "2026-07-01T00:00:00Z",
};
const naResponse: NextActionsResponse = {
  market: "all", symbol: null, count: 0, scan_limit: 200, items: [],
};

afterEach(() => vi.unstubAllGlobals());

test("fetchRetrospectives sends filters + pagination with credentials", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => listResponse });
  vi.stubGlobal("fetch", fetchMock);

  await expect(
    fetchRetrospectives({
      market: "us", triggerType: "fill", rootCauseClass: "analysis",
      symbol: "AAPL", limit: 10, offset: 20,
    }),
  ).resolves.toEqual(listResponse);

  const [url, init] = fetchMock.mock.calls[0]!;
  expect(init).toEqual({ credentials: "include" });
  const params = new URLSearchParams(String(url).split("?")[1]);
  expect(String(url)).toMatch(/^\/trading\/api\/invest\/retrospectives\?/);
  expect(params.get("market")).toBe("us");
  expect(params.get("trigger_type")).toBe("fill");
  expect(params.get("root_cause_class")).toBe("analysis");
  expect(params.get("symbol")).toBe("AAPL");
  expect(params.get("limit")).toBe("10");
  expect(params.get("offset")).toBe("20");
});

test("fetchOpenNextActions hits next-actions with scope", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => naResponse });
  vi.stubGlobal("fetch", fetchMock);

  await expect(fetchOpenNextActions("kr", "005930")).resolves.toEqual(naResponse);
  const [url] = fetchMock.mock.calls[0]!;
  expect(String(url)).toMatch(/^\/trading\/api\/invest\/retrospectives\/next-actions\?/);
  const params = new URLSearchParams(String(url).split("?")[1]);
  expect(params.get("market")).toBe("kr");
  expect(params.get("symbol")).toBe("005930");
});

test("rejects non-OK responses", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 401 }));
  await expect(fetchRetrospectives({})).rejects.toThrow("retrospectives 401");
});

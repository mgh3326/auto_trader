import { afterEach, expect, test, vi } from "vitest";
import {
  fetchOpenNextActions,
  fetchRetrospectiveActions,
  fetchRetrospectives,
} from "../api/retrospectives";
import type {
  NextActionsResponse,
  RetrospectiveActionsResponse,
  RetrospectivesResponse,
} from "../types/retrospectives";

const listResponse: RetrospectivesResponse = {
  market: "kr", trigger_type: null, root_cause_class: null, symbol: null,
  outcome_filter: null, q: null, kst_date_from: null, kst_date_to: null,
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

test("fetchRetrospectives sends outcome/symbol-search/date-range filters (ROB-691)", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => listResponse });
  vi.stubGlobal("fetch", fetchMock);

  await fetchRetrospectives({
    outcomeFilter: "win", q: "005", dateFrom: "2026-07-01", dateTo: "2026-07-04",
  });

  const [url] = fetchMock.mock.calls[0]!;
  const params = new URLSearchParams(String(url).split("?")[1]);
  expect(params.get("outcome_filter")).toBe("win");
  expect(params.get("q")).toBe("005");
  expect(params.get("kst_date_from")).toBe("2026-07-01");
  expect(params.get("kst_date_to")).toBe("2026-07-04");
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

const actionsResponse: RetrospectiveActionsResponse = {
  total: 0,
  count: 0,
  limit: 50,
  offset: 0,
  as_of: "2026-07-15T00:00:00Z",
  items: [],
};

test("fetchRetrospectiveActions hits canonical /actions endpoint with explicit active status (ROB-885)", async () => {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => actionsResponse,
  });
  vi.stubGlobal("fetch", fetchMock);

  await expect(fetchRetrospectiveActions({})).resolves.toEqual(actionsResponse);

  const [url, init] = fetchMock.mock.calls[0]!;
  expect(init).toEqual({ credentials: "include" });
  expect(String(url)).toMatch(/^\/trading\/api\/invest\/retrospectives\/actions\?/);
  const params = new URLSearchParams(String(url).split("?")[1]);
  // ROB-885 — always send explicit active status, never rely on server default.
  expect(params.get("status")).toBe("open,in_progress");
  expect(params.get("market")).toBe("all");
});

test("fetchRetrospectiveActions always sends status=open,in_progress even when other filters vary", async () => {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => actionsResponse,
  });
  vi.stubGlobal("fetch", fetchMock);

  await fetchRetrospectiveActions({
    market: "us",
    triggerType: "fill",
    outcomeFilter: "win",
    q: "AAPL",
    dateFrom: "2026-07-01",
    dateTo: "2026-07-04",
    limit: 10,
    offset: 20,
  });

  const [url] = fetchMock.mock.calls[0]!;
  const params = new URLSearchParams(String(url).split("?")[1]);
  expect(params.get("status")).toBe("open,in_progress");
  expect(params.get("market")).toBe("us");
  expect(params.get("trigger_type")).toBe("fill");
  expect(params.get("outcome_filter")).toBe("win");
  expect(params.get("q")).toBe("AAPL");
  expect(params.get("kst_date_from")).toBe("2026-07-01");
  expect(params.get("kst_date_to")).toBe("2026-07-04");
  expect(params.get("limit")).toBe("10");
  expect(params.get("offset")).toBe("20");
});

test("fetchRetrospectiveActions forwards pagination offset for progressive expansion (ROB-885)", async () => {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => actionsResponse,
  });
  vi.stubGlobal("fetch", fetchMock);

  await fetchRetrospectiveActions({ offset: 40 });

  const [url] = fetchMock.mock.calls[0]!;
  const params = new URLSearchParams(String(url).split("?")[1]);
  expect(params.get("offset")).toBe("40");
});

test("fetchRetrospectiveActions rejects non-OK responses", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 500 }));
  await expect(fetchRetrospectiveActions({})).rejects.toThrow(
    "retrospectives actions 500",
  );
});

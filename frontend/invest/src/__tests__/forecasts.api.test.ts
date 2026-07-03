import { afterEach, expect, test, vi } from "vitest";
import {
  fetchClosedForecasts,
  fetchForecastCalibration,
  fetchOpenForecasts,
} from "../api/forecasts";
import type { CalibrationResponse, ForecastListResponse } from "../types/forecasts";

const calib: CalibrationResponse = {
  group_by: "created_by", created_by: null, symbol: null, instrument_type: null,
  days: null, count: 0, groups: [], as_of: "2026-07-01T00:00:00Z",
};
const list: ForecastListResponse = {
  kind: "open", symbol: null, created_by: null, instrument_type: null,
  count: 0, items: [], as_of: "2026-07-01T00:00:00Z",
};

afterEach(() => vi.unstubAllGlobals());

test("fetchForecastCalibration sends group_by + filters with credentials", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => calib });
  vi.stubGlobal("fetch", fetchMock);

  await expect(
    fetchForecastCalibration({ groupBy: "model_label", symbol: "AAPL", days: 30 }),
  ).resolves.toEqual(calib);

  const [url, init] = fetchMock.mock.calls[0]!;
  expect(init).toEqual({ credentials: "include" });
  expect(String(url)).toMatch(/^\/trading\/api\/invest\/forecasts\/calibration\?/);
  const params = new URLSearchParams(String(url).split("?")[1]);
  expect(params.get("group_by")).toBe("model_label");
  expect(params.get("symbol")).toBe("AAPL");
  expect(params.get("days")).toBe("30");
});

test("fetchOpenForecasts / fetchClosedForecasts hit their endpoints", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => list });
  vi.stubGlobal("fetch", fetchMock);

  await fetchOpenForecasts({ limit: 20 });
  expect(String(fetchMock.mock.calls[0]![0])).toMatch(
    /^\/trading\/api\/invest\/forecasts\/open\?/,
  );

  await fetchClosedForecasts({ symbol: "005930" });
  const closedUrl = String(fetchMock.mock.calls[1]![0]);
  expect(closedUrl).toMatch(/^\/trading\/api\/invest\/forecasts\/closed\?/);
  expect(new URLSearchParams(closedUrl.split("?")[1]).get("symbol")).toBe("005930");
});

test("rejects non-OK responses", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 401 }));
  await expect(fetchForecastCalibration()).rejects.toThrow("forecast calibration 401");
});

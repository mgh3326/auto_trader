import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";
import { ForecastCalibrationPanel } from "../components/insights/ForecastCalibrationPanel";
import type { CalibrationResponse, ForecastListResponse } from "../types/forecasts";

const calib: CalibrationResponse = {
  group_by: "created_by", created_by: null, symbol: null, instrument_type: null,
  days: null, count: 1, as_of: "2026-07-01T00:00:00Z",
  groups: [{
    group: "hermes", sample_size: 4, hits: 3, misses: 1,
    hit_rate: 0.75, avg_brier_score: 0.18, avg_probability: 0.8,
    calibration_gap: 0.05,
  }],
};
const open: ForecastListResponse = {
  kind: "open", symbol: null, created_by: null, instrument_type: null,
  count: 1, as_of: "2026-07-01T00:00:00Z",
  items: [{
    id: 1, forecast_id: "f1", correlation_id: null, created_by: "claude",
    session_label: null, model_label: null, symbol: "005930",
    instrument_type: "equity_kr",
    forecast_target: { kind: "price_target", direction: "at_or_above", target_price: 80000 },
    horizon: "D+5",
    probability: 0.6, probability_range_low: null, probability_range_high: null,
    resolution_source: null, review_date: "2026-07-10", status: "open",
    outcome: null, observed_value: null, resolved_at: null, brier_score: null,
    created_at: "2026-07-01T00:00:00Z",
  }],
};
const closed: ForecastListResponse = {
  kind: "closed", symbol: null, created_by: null, instrument_type: null,
  count: 1, as_of: "2026-07-01T00:00:00Z",
  items: [{
    id: 2, forecast_id: "f2", correlation_id: null, created_by: "claude",
    session_label: null, model_label: null, symbol: "AAPL",
    instrument_type: "equity_us",
    forecast_target: { kind: "price_target", direction: "at_or_below", target_price: 180 },
    horizon: null,
    probability: 0.7, probability_range_low: null, probability_range_high: null,
    resolution_source: null, review_date: "2026-06-20", status: "closed",
    outcome: true, observed_value: 175.5, resolved_at: "2026-06-21T00:00:00Z",
    brier_score: 0.09, created_at: "2026-06-10T00:00:00Z",
  }],
};

afterEach(() => vi.unstubAllGlobals());

test("renders calibration table, due queue and recent scored results", async () => {
  const fetchMock = vi.fn((url: string) => {
    const u = String(url);
    const body = u.includes("/calibration") ? calib : u.includes("/open") ? open : closed;
    return Promise.resolve({ ok: true, json: async () => body });
  });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(
    <MemoryRouter>
      <ForecastCalibrationPanel />
    </MemoryRouter>,
  );

  // calibration cohort row
  await waitFor(() => expect(screen.getByText("hermes")).toBeInTheDocument());
  // due-queue open forecast (symbol link)
  expect(screen.getByText("005930")).toBeInTheDocument();
  // recent scored result: outcome badge + symbol
  expect(screen.getByText("적중")).toBeInTheDocument();
  expect(screen.getByText("AAPL")).toBeInTheDocument();

  // ROB-673: open forecast surfaces its target + horizon
  expect(screen.getByText(/목표 ≥ ₩80,000/)).toBeInTheDocument();
  expect(screen.getByText(/D\+5/)).toBeInTheDocument();
  // ROB-673: closed forecast surfaces target + realized observed value
  expect(screen.getByText(/목표 ≤ \$180\.00/)).toBeInTheDocument();
  expect(screen.getByText(/실현 \$175\.50/)).toBeInTheDocument();

  // ROB-675: sample_size=4 (<5) trips the small-sample guard + surfaces misses
  expect(screen.getByText(/소표본/)).toBeInTheDocument();
  expect(screen.getByText(/실패 1/)).toBeInTheDocument();
});

test("days control refetches calibration with the selected window (ROB-674)", async () => {
  const calls: string[] = [];
  const fetchMock = vi.fn((url: string) => {
    const u = String(url);
    calls.push(u);
    const body = u.includes("/calibration") ? calib : u.includes("/open") ? open : closed;
    return Promise.resolve({ ok: true, json: async () => body });
  });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(
    <MemoryRouter>
      <ForecastCalibrationPanel />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getByText("hermes")).toBeInTheDocument());

  // default window is 90일
  expect(calls.some((u) => u.includes("/calibration") && u.includes("days=90"))).toBe(true);

  // 30일 → refetch with days=30
  fireEvent.click(screen.getByRole("button", { name: "30일" }));
  await waitFor(() =>
    expect(calls.some((u) => u.includes("/calibration") && u.includes("days=30"))).toBe(true),
  );

  // 전체 → omit days param entirely
  fireEvent.click(screen.getByRole("button", { name: "전체" }));
  await waitFor(() =>
    expect(calls.some((u) => u.includes("/calibration") && !u.includes("days="))).toBe(true),
  );
});

test("calibration table sorts client-side on header click (ROB-675)", async () => {
  const multi: CalibrationResponse = {
    ...calib,
    count: 2,
    groups: [
      { group: "alpha", sample_size: 10, hits: 6, misses: 4, hit_rate: 0.6, avg_brier_score: 0.2, avg_probability: 0.7, calibration_gap: 0.1 },
      { group: "beta", sample_size: 8, hits: 7, misses: 1, hit_rate: 0.875, avg_brier_score: 0.1, avg_probability: 0.8, calibration_gap: -0.075 },
    ],
  };
  const fetchMock = vi.fn((url: string) => {
    const u = String(url);
    const b = u.includes("/calibration") ? multi : u.includes("/open") ? open : closed;
    return Promise.resolve({ ok: true, json: async () => b });
  });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(
    <MemoryRouter>
      <ForecastCalibrationPanel />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getByText("alpha")).toBeInTheDocument());

  const bodyGroups = () =>
    screen
      .getAllByRole("row")
      .slice(1)
      .map((row) => within(row).getAllByRole("cell")[0]?.textContent ?? "");

  // default preserves server order
  expect(bodyGroups()).toEqual(["alpha", "beta"]);

  // 적중률 desc: beta (0.875) before alpha (0.6)
  fireEvent.click(screen.getByRole("columnheader", { name: /적중률/ }));
  expect(bodyGroups()).toEqual(["beta", "alpha"]);

  // toggle → asc: alpha before beta
  fireEvent.click(screen.getByRole("columnheader", { name: /적중률/ }));
  expect(bodyGroups()).toEqual(["alpha", "beta"]);
});

import { render, screen, waitFor } from "@testing-library/react";
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
    instrument_type: "equity_kr", forecast_target: null, horizon: null,
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
    instrument_type: "equity_us", forecast_target: null, horizon: null,
    probability: 0.7, probability_range_low: null, probability_range_high: null,
    resolution_source: null, review_date: "2026-06-20", status: "closed",
    outcome: true, observed_value: null, resolved_at: "2026-06-21T00:00:00Z",
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
});

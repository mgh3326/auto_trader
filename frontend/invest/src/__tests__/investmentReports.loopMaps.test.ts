import { describe, it, expect } from "vitest";
import {
  normalizeForecastLink,
  normalizeRetrospectiveLink,
} from "../api/investmentReports";

describe("ROB-715 loop-map normalizers", () => {
  it("normalizes a forecast link snake→camel", () => {
    const out = normalizeForecastLink({
      forecast_id: "f1",
      status: "closed",
      outcome: true,
      review_date: "2026-07-20",
      direction: "at_or_above",
      target_price: 200000,
      probability: 0.6,
      brier_score: 0.09,
      resolution_source: "ohlcv_day",
    });
    expect(out.forecastId).toBe("f1");
    expect(out.status).toBe("closed");
    expect(out.outcome).toBe(true);
    expect(out.targetPrice).toBe(200000);
    expect(out.brierScore).toBe(0.09);
  });

  it("normalizes a retrospective link", () => {
    const out = normalizeRetrospectiveLink({
      retrospective_id: 1,
      outcome: "filled",
      lesson: "cut late",
      root_cause_class: "execution",
      pnl_pct: -3.5,
    });
    expect(out.outcome).toBe("filled");
    expect(out.lesson).toBe("cut late");
    expect(out.pnlPct).toBe(-3.5);
  });
});
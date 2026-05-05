// frontend/trading-decision/src/__tests__/pages/PortfolioActionsPage.test.tsx
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import PortfolioActionsPage from "../../pages/PortfolioActionsPage";
import * as api from "../../api/portfolioActions";

const fixture = {
  generated_at: "2026-05-05T00:00:00+00:00",
  total: 1,
  candidates: [
    {
      symbol: "KRW-SOL",
      name: "솔라나",
      market: "CRYPTO" as const,
      instrument_type: "crypto",
      position_weight_pct: 29.75,
      profit_rate: -12.25,
      quantity: 10,
      sellable_quantity: 8,
      staked_quantity: 2,
      latest_research_session_id: 32,
      summary_decision: "hold" as const,
      summary_confidence: 55,
      market_verdict: "neutral" as const,
      nearest_support_pct: -1.93,
      nearest_resistance_pct: 1.22,
      journal_status: "missing" as const,
      candidate_action: "trim" as const,
      suggested_trim_pct: 20,
      reason_codes: ["overweight"],
      missing_context_codes: ["journal_missing"],
    },
  ],
  warnings: [],
};

describe("PortfolioActionsPage", () => {
  beforeEach(() => {
    vi.spyOn(api, "getPortfolioActions").mockResolvedValue(fixture);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("fetches and renders candidates", async () => {
    render(
      <MemoryRouter>
        <PortfolioActionsPage />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByText("KRW-SOL")).toBeInTheDocument();
    });
    expect(api.getPortfolioActions).toHaveBeenCalledWith(undefined);
  });

  it("filters by market", async () => {
    render(
      <MemoryRouter>
        <PortfolioActionsPage />
      </MemoryRouter>,
    );
    const select = await screen.findByLabelText(/시장/);
    (select as HTMLSelectElement).value = "CRYPTO";
    select.dispatchEvent(new Event("change", { bubbles: true }));
    await waitFor(() => {
      expect(api.getPortfolioActions).toHaveBeenCalledWith("CRYPTO");
    });
  });

  it("shows empty state when no candidates", async () => {
    (api.getPortfolioActions as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ...fixture,
      total: 0,
      candidates: [],
    });
    render(
      <MemoryRouter>
        <PortfolioActionsPage />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByText(/보유 종목이 없습니다/)).toBeInTheDocument();
    });
  });
});

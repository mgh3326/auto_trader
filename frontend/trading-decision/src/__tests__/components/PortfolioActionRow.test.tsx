// frontend/trading-decision/src/__tests__/components/PortfolioActionRow.test.tsx
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import PortfolioActionRow from "../../components/PortfolioActionRow";
import type { PortfolioActionCandidate } from "../../api/types";

const sample: PortfolioActionCandidate = {
  symbol: "KRW-SOL",
  name: "솔라나",
  market: "CRYPTO",
  instrument_type: "crypto",
  position_weight_pct: 29.75,
  profit_rate: -12.25,
  quantity: 10,
  sellable_quantity: 8,
  staked_quantity: 2,
  latest_research_session_id: 32,
  summary_decision: "hold",
  summary_confidence: 55,
  market_verdict: "neutral",
  nearest_support_pct: -1.93,
  nearest_resistance_pct: 1.22,
  journal_status: "missing",
  candidate_action: "trim",
  suggested_trim_pct: 20,
  reason_codes: ["overweight", "research_not_bullish", "near_resistance"],
  missing_context_codes: ["journal_missing"],
};

function renderRow(candidate = sample) {
  return render(
    <MemoryRouter>
      <table>
        <tbody>
          <PortfolioActionRow candidate={candidate} />
        </tbody>
      </table>
    </MemoryRouter>,
  );
}

describe("PortfolioActionRow", () => {
  it("renders symbol, action badge, and reasons", () => {
    renderRow();
    expect(screen.getByText("KRW-SOL")).toBeInTheDocument();
    expect(screen.getByText(/부분 축소/)).toBeInTheDocument();
    expect(screen.getByText(/비중 과대/)).toBeInTheDocument();
    expect(screen.getByText(/저항 근접/)).toBeInTheDocument();
  });

  it("links Research to research session route", () => {
    renderRow();
    const link = screen.getByRole("link", { name: /Research 보기/ });
    expect(link).toHaveAttribute(
      "href",
      "/trading/decisions/research/sessions/32/summary",
    );
  });

  it("shows missing context warning separately from reasons", () => {
    renderRow();
    expect(screen.getByText(/Journal 미작성/)).toBeInTheDocument();
  });

  it("links Order Preview to placeholder route with symbol", () => {
    renderRow();
    const link = screen.getByRole("link", { name: /주문 Preview/ });
    expect(link).toHaveAttribute(
      "href",
      "/trading/decisions/orders/preview?symbol=KRW-SOL",
    );
  });
});

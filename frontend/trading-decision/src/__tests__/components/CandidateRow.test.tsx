// frontend/trading-decision/src/__tests__/components/CandidateRow.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import CandidateRow from "../../components/CandidateRow";
import type { ScreenedCandidate } from "../../api/types";

const sample: ScreenedCandidate = {
  symbol: "KRW-ETH",
  name: "이더리움",
  market: "crypto",
  instrument_type: "crypto",
  price: 4_500_000,
  change_rate: 2.1,
  volume: 1234,
  trade_amount_24h: 0.0,
  volume_ratio: null,
  rsi: 28.5,
  market_cap: null,
  per: null,
  pbr: null,
  sector: null,
  is_held: false,
  held_quantity: null,
  latest_research_session_id: null,
  research_status: null,
  data_warnings: ["KRW-BTC ticker not found"],
};

describe("CandidateRow", () => {
  it("renders symbol, RSI, and data warnings", () => {
    render(
      <table><tbody>
        <CandidateRow candidate={sample} onStartResearch={vi.fn()} />
      </tbody></table>,
    );
    expect(screen.getByText("KRW-ETH")).toBeInTheDocument();
    expect(screen.getByText("28.50")).toBeInTheDocument();
    expect(screen.getByText(/KRW-BTC ticker not found/)).toBeInTheDocument();
  });

  it("shows held indicator for held positions", () => {
    render(
      <table><tbody>
        <CandidateRow
          candidate={{ ...sample, is_held: true }}
          onStartResearch={vi.fn()}
        />
      </tbody></table>,
    );
    expect(screen.getByText(/보유 중/)).toBeInTheDocument();
  });

  it("invokes onStartResearch with the candidate when button clicked", () => {
    const handler = vi.fn();
    render(
      <table><tbody>
        <CandidateRow candidate={sample} onStartResearch={handler} />
      </tbody></table>,
    );
    fireEvent.click(screen.getByRole("button", { name: /Research Session 시작/ }));
    expect(handler).toHaveBeenCalledWith(sample);
  });
});

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import CandidatesPage from "../../pages/CandidatesPage";
import * as candidatesApi from "../../api/candidates";
import * as researchApi from "../../api/researchPipeline";

const fixture = {
  generated_at: "2026-05-05T00:00:00+00:00",
  market: "crypto",
  strategy: "oversold",
  sort_by: "rsi",
  total: 1,
  candidates: [
    {
      symbol: "KRW-ETH",
      name: "이더리움",
      market: "crypto",
      instrument_type: "crypto",
      price: 4500000,
      change_rate: 2.1,
      volume: 1234,
      trade_amount_24h: 0,
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
      data_warnings: [],
    },
  ],
  warnings: ["rsi_enrichment_skipped"],
  rsi_enrichment_attempted: 0,
  rsi_enrichment_succeeded: 0,
};

describe("CandidatesPage", () => {
  beforeEach(() => {
    vi.spyOn(candidatesApi, "screenCandidates").mockResolvedValue(fixture);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("runs screening on submit and renders candidates + warnings", async () => {
    render(
      <MemoryRouter>
        <CandidatesPage />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: /후보 스캔/ }));
    await waitFor(() => {
      expect(screen.getByText("KRW-ETH")).toBeInTheDocument();
    });
    expect(screen.getByText(/rsi_enrichment_skipped/)).toBeInTheDocument();
  });

  it("starts Research Session via API and shows confirmation", async () => {
    vi.spyOn(researchApi, "createSession").mockResolvedValue({
      session_id: 99,
      status: "running",
      started_at: "2026-05-05T00:00:01+00:00",
    });
    render(
      <MemoryRouter>
        <CandidatesPage />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: /후보 스캔/ }));
    await waitFor(() => {
      expect(screen.getByText("KRW-ETH")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: /Research Session 시작/ }));
    await waitFor(() => {
      expect(researchApi.createSession).toHaveBeenCalledWith({
        symbol: "KRW-ETH",
        name: "이더리움",
        instrument_type: "crypto",
        triggered_by: "user",
      });
    });
    await waitFor(() => {
      expect(screen.getByText(/#99/)).toBeInTheDocument();
    });
  });
});

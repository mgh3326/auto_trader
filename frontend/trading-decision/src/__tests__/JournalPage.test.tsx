// frontend/trading-decision/src/__tests__/JournalPage.test.tsx
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

vi.mock("../api/tradeJournals", () => ({
  getJournalCoverage: vi.fn(),
  getJournalRetrospective: vi.fn(),
  createJournal: vi.fn(),
  updateJournal: vi.fn(),
}));

import * as api from "../api/tradeJournals";
import JournalPage from "../pages/JournalPage";

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("JournalPage", () => {
  beforeEach(() => {
    (api.getJournalCoverage as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      generated_at: "2026-05-06T00:00:00Z",
      total: 1,
      rows: [
        {
          symbol: "005930",
          name: "삼성전자",
          market: "KR",
          instrument_type: "equity_kr",
          quantity: 10,
          position_weight_pct: 12.5,
          journal_status: "missing",
          journal_id: null,
          thesis: null,
          target_price: null,
          stop_loss: null,
          min_hold_days: null,
          hold_until: null,
          latest_research_session_id: null,
          latest_research_summary_id: null,
          latest_summary_decision: null,
          thesis_conflict_with_summary: false,
        },
      ],
      warnings: [],
    });
  });

  it("renders coverage row with the missing-journal status", async () => {
    render(
      <MemoryRouter>
        <JournalPage />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByText("005930")).toBeInTheDocument();
    });
    expect(screen.getByText("삼성전자")).toBeInTheDocument();
    expect(screen.getByText(/미작성/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /작성/ })).toBeInTheDocument();
  });
});

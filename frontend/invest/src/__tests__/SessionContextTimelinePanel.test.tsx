import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionContextTimelinePanel } from "../components/insights/SessionContextTimelinePanel";

afterEach(() => vi.unstubAllGlobals());

const body = {
  success: true as const,
  count: 1,
  filters: {
    market: null,
    account_scope: null,
    kst_date_from: null,
    entry_type: null,
    limit: 15,
  },
  entries: [
    {
      entry_uuid: "e-1",
      kst_date: "2026-07-03",
      market: "kr",
      account_scope: null,
      entry_type: "handoff_note",
      title: "다음 세션 인계",
      body: "삼성전자 매수 래더 절반 남음",
      refs: { symbols: ["005930"], order_id: "ORD-1", report_uuid: "rep-abcdef1234" },
      created_by: "claude",
      session_label: null,
      created_at: "2026-07-03T09:00:00+00:00",
    },
  ],
};

describe("SessionContextTimelinePanel", () => {
  it("renders recent handoff entries with entry_type chip", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => body,
      })) as unknown as typeof fetch,
    );

    render(
      <MemoryRouter>
        <SessionContextTimelinePanel />
      </MemoryRouter>,
    );

    await waitFor(() => screen.getByText("다음 세션 인계"));
    expect(screen.getByTestId("session-context-timeline-panel")).toBeTruthy();
    expect(screen.getByText("handoff_note")).toBeTruthy();

    // ROB-673: refs crosslinks — symbol links to stock detail, provenance surfaced
    const symLink = screen.getByRole("link", { name: "005930" });
    expect(symLink).toHaveAttribute("href", "/stocks/kr/005930");
    expect(screen.getByText(/주문 ORD-1/)).toBeInTheDocument();
  });
});

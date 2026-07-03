import { render, screen, waitFor } from "@testing-library/react";
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
      refs: {},
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

    render(<SessionContextTimelinePanel />);

    await waitFor(() => screen.getByText("다음 세션 인계"));
    expect(screen.getByTestId("session-context-timeline-panel")).toBeTruthy();
    expect(screen.getByText("handoff_note")).toBeTruthy();
  });
});

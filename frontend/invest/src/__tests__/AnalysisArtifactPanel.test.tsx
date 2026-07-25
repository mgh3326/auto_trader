import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AnalysisArtifactPanel } from "../components/insights/AnalysisArtifactPanel";

afterEach(() => vi.unstubAllGlobals());

const listBody = {
  success: true as const,
  count: 1,
  filters: {
    market: null,
    kind: null,
    symbol: null,
    since: null,
    include_stale: true,
    limit: 20,
    correlation_id: null,
    account_scope: null,
  },
  artifacts: [
    {
      id: 3,
      artifact_uuid: "u-3",
      market: "kr",
      kind: "screening_ranking",
      title: "KR 스크리닝",
      symbols: ["005930"],
      as_of: "2026-07-03T00:00:00+00:00",
      valid_until: null,
      session_label: null,
      correlation_id: null,
      account_scope: null,
      content_hash: "abc123def456",
      version: 2,
      readiness_label: "ready_for_order_review",
      is_stale: true,
      created_by: "claude",
      created_at: "2026-07-03T00:00:00+00:00",
    },
  ],
};

describe("AnalysisArtifactPanel", () => {
  it("renders artifacts with stale badge and version", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      const u = String(url);
      const body = u.includes("/artifacts/") && !u.endsWith("/3")
        ? listBody
        : { success: true, artifact: { ...listBody.artifacts[0], payload: { k: "v" } } };
      return { ok: true, status: 200, json: async () => body };
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    render(
      <MemoryRouter>
        <AnalysisArtifactPanel />
      </MemoryRouter>,
    );

    await waitFor(() => screen.getByText("KR 스크리닝"));
    expect(screen.getByTestId("analysis-artifact-panel")).toBeTruthy();
    expect(screen.getByText(/stale/i)).toBeTruthy();
    expect(screen.getByText(/v2/)).toBeTruthy();

    // ROB-673: 종목 column links each symbol to its stock detail page
    const symLink = screen.getByRole("link", { name: "005930" });
    expect(symLink).toHaveAttribute("href", "/stocks/kr/005930");
  });

  it("refetches with the corresponding query param when a filter changes (ROB-674)", async () => {
    const calls: string[] = [];
    const fetchMock = vi.fn(async (url: string) => {
      const u = String(url);
      calls.push(u);
      const body = u.includes("/artifacts/") && !u.endsWith("/3")
        ? listBody
        : { success: true, artifact: { ...listBody.artifacts[0], payload: { k: "v" } } };
      return { ok: true, status: 200, json: async () => body };
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    render(
      <MemoryRouter>
        <AnalysisArtifactPanel />
      </MemoryRouter>,
    );
    await waitFor(() => screen.getByText("KR 스크리닝"));

    fireEvent.change(screen.getByLabelText("시장 필터"), { target: { value: "us" } });
    await waitFor(() => expect(calls.some((u) => u.includes("market=us"))).toBe(true));

    fireEvent.change(screen.getByLabelText("종류 필터"), { target: { value: "briefing" } });
    await waitFor(() => expect(calls.some((u) => u.includes("kind=briefing"))).toBe(true));

    fireEvent.change(screen.getByLabelText("준비상태 필터"), { target: { value: "blocked" } });
    await waitFor(() => expect(calls.some((u) => u.includes("readiness_label=blocked"))).toBe(true));
  });
});

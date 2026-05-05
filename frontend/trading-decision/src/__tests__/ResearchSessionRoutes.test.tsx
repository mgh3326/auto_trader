import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, Navigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import ResearchSessionLayout from "../pages/research/ResearchSessionLayout";
import ResearchSummaryPage from "../pages/research/ResearchSummaryPage";
import ResearchMarketPage from "../pages/research/ResearchMarketPage";
import ResearchNewsPage from "../pages/research/ResearchNewsPage";
import ResearchFundamentalsPage from "../pages/research/ResearchFundamentalsPage";
import ResearchSessionNotFoundPage from "../pages/research/ResearchSessionNotFoundPage";
import { makeSessionFull } from "../test/fixtures/research";
import { mockFetch } from "../test/server";

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route
          path="/research/sessions/:sessionId"
          element={<ResearchSessionLayout />}
        >
          <Route index element={<Navigate to="summary" replace />} />
          <Route path="summary" element={<ResearchSummaryPage />} />
          <Route path="market" element={<ResearchMarketPage />} />
          <Route path="news" element={<ResearchNewsPage />} />
          <Route path="fundamentals" element={<ResearchFundamentalsPage />} />
          <Route path="*" element={<ResearchSessionNotFoundPage />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

function mockSessionOk() {
  mockFetch({
    "/trading/api/research-pipeline/sessions/1?include=full": () =>
      new Response(JSON.stringify(makeSessionFull())),
  });
}

describe("Research session stage routes", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("redirects /research/sessions/:sessionId to /summary", async () => {
    mockSessionOk();
    renderAt("/research/sessions/1");
    await waitFor(() =>
      expect(
        screen.getByRole("link", { name: /종합/, current: "page" }),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText("매수")).toBeInTheDocument();
  });

  it("renders the summary page at /summary", async () => {
    mockSessionOk();
    renderAt("/research/sessions/1/summary");
    await waitFor(() =>
      expect(screen.getByText("매수")).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("complementary", { name: "인용된 단계" }),
    ).toBeInTheDocument();
  });

  it("renders the market page at /market", async () => {
    mockSessionOk();
    renderAt("/research/sessions/1/market");
    await waitFor(() =>
      expect(screen.getByText("종가")).toBeInTheDocument(),
    );
  });

  it("renders the news page at /news", async () => {
    mockSessionOk();
    renderAt("/research/sessions/1/news");
    await waitFor(() =>
      expect(screen.getByText("헤드라인 수")).toBeInTheDocument(),
    );
  });

  it("renders the fundamentals page at /fundamentals", async () => {
    mockSessionOk();
    renderAt("/research/sessions/1/fundamentals");
    await waitFor(() =>
      expect(screen.getByText("PER")).toBeInTheDocument(),
    );
  });

  it("does not render a social link in the stage navigation", async () => {
    mockSessionOk();
    renderAt("/research/sessions/1/summary");
    await waitFor(() =>
      expect(screen.getByText("매수")).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole("link", { name: /소셜/ }),
    ).not.toBeInTheDocument();
  });

  it("renders the not-found body for unknown stage segments", async () => {
    mockSessionOk();
    renderAt("/research/sessions/1/bogus");
    await waitFor(() =>
      expect(
        screen.getByText(/잘못된 단계 경로 입니다/),
      ).toBeInTheDocument(),
    );
  });

  it("shows not_found banner when session 404s", async () => {
    mockFetch({
      "/trading/api/research-pipeline/sessions/999?include=full": () =>
        new Response(JSON.stringify({ detail: "session_not_found" }), {
          status: 404,
        }),
    });
    renderAt("/research/sessions/999/summary");
    await waitFor(() =>
      expect(
        screen.getByText(/세션을 찾을 수 없습니다/),
      ).toBeInTheDocument(),
    );
  });

  it("navigates to the news stage when its nav link is clicked", async () => {
    mockSessionOk();
    renderAt("/research/sessions/1/summary");
    await waitFor(() =>
      expect(screen.getByText("매수")).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByRole("link", { name: /뉴스/ }));
    await waitFor(() =>
      expect(screen.getByText("헤드라인 수")).toBeInTheDocument(),
    );
  });

  it("navigates from the citation sidebar button to the cited stage page", async () => {
    mockSessionOk();
    renderAt("/research/sessions/1/summary");
    const sidebar = await waitFor(() =>
      screen.getByRole("complementary", { name: "인용된 단계" }),
    );
    await userEvent.click(
      within(sidebar).getByRole("button", { name: /시장 단계로 이동/ }),
    );
    await waitFor(() =>
      expect(screen.getByText("종가")).toBeInTheDocument(),
    );
  });
});

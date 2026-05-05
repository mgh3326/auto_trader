import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import ResearchSessionDetailPage from "../pages/ResearchSessionDetailPage";
import { makeSessionFull } from "../test/fixtures/research";
import { mockFetch } from "../test/server";

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route
          path="/research/sessions/:sessionId"
          element={<ResearchSessionDetailPage />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe("ResearchSessionDetailPage", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("renders summary tab on success", async () => {
    mockFetch({
      "/trading/api/research-pipeline/sessions/1?include=full": () =>
        new Response(JSON.stringify(makeSessionFull())),
    });
    renderAt("/research/sessions/1");
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: /종합/ })).toBeInTheDocument(),
    );
    expect(screen.getByText("매수")).toBeInTheDocument();
  });

  it("shows not_found when 404", async () => {
    mockFetch({
      "/trading/api/research-pipeline/sessions/999?include=full": () =>
        new Response(JSON.stringify({ detail: "session_not_found" }), {
          status: 404,
        }),
    });
    renderAt("/research/sessions/999");
    await waitFor(() =>
      expect(screen.getByText(/세션을 찾을 수 없습니다/)).toBeInTheDocument(),
    );
  });

  it("switches to social tab and renders placeholder", async () => {
    mockFetch({
      "/trading/api/research-pipeline/sessions/1?include=full": () =>
        new Response(JSON.stringify(makeSessionFull())),
    });
    renderAt("/research/sessions/1");
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: /소셜/ })).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByRole("tab", { name: /소셜/ }));
    expect(
      screen.getByText(/소셜 신호 분석은 준비 중입니다/),
    ).toBeInTheDocument();
  });

  it("renders citation sidebar with support direction", async () => {
    mockFetch({
      "/trading/api/research-pipeline/sessions/1?include=full": () =>
        new Response(JSON.stringify(makeSessionFull())),
    });
    renderAt("/research/sessions/1");
    const sidebar = await waitFor(() =>
      screen.getByRole("complementary", { name: "인용된 단계" }),
    );
    expect(within(sidebar).getByText("지지")).toBeInTheDocument();
    expect(within(sidebar).getByText(/시장/)).toBeInTheDocument();
  });
});

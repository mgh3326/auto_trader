import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import SymbolTimelinePage from "../pages/SymbolTimelinePage";
import { makeSymbolTimeline } from "../test/fixtures/research";
import { mockFetch } from "../test/server";

describe("SymbolTimelinePage", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("renders entries and chart", async () => {
    mockFetch({
      "/trading/api/research-pipeline/symbols/AAPL/timeline?days=30": () =>
        new Response(JSON.stringify(makeSymbolTimeline())),
    });
    render(
      <MemoryRouter initialEntries={["/research/symbols/AAPL/timeline"]}>
        <Routes>
          <Route
            path="/research/symbols/:symbol/timeline"
            element={<SymbolTimelinePage />}
          />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("AAPL")).toBeInTheDocument());
    expect(
      screen.getByRole("img", { name: /평결 변화 미니 차트/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(/매수/)).toBeInTheDocument();
  });
});

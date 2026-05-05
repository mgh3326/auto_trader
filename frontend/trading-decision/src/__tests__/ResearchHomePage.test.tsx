import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import ResearchHomePage from "../pages/ResearchHomePage";
import {
  makeCreateResponse,
  makeSessionListItem,
} from "../test/fixtures/research";
import { mockFetch } from "../test/server";

describe("ResearchHomePage", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("renders recent sessions", async () => {
    mockFetch({
      "/trading/api/research-pipeline/sessions?limit=20": () =>
        new Response(JSON.stringify([makeSessionListItem({ id: 1 })])),
    });
    render(
      <MemoryRouter initialEntries={["/research"]}>
        <Routes>
          <Route path="/research" element={<ResearchHomePage />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(screen.getByText(/세션 시작/)).toBeInTheDocument(),
    );
    expect(screen.getByRole("row", { name: /1/ })).toBeInTheDocument();
  });

  it("submits start form and navigates to detail", async () => {
    const { calls } = mockFetch({
      "/trading/api/research-pipeline/sessions?limit=20": () =>
        new Response(JSON.stringify([])),
      "/trading/api/research-pipeline/sessions": () =>
        new Response(
          JSON.stringify(makeCreateResponse({ session_id: 77 })),
          { status: 201 },
        ),
    });
    render(
      <MemoryRouter initialEntries={["/research"]}>
        <Routes>
          <Route path="/research" element={<ResearchHomePage />} />
          <Route
            path="/research/sessions/:sessionId"
            element={<div>detail-77</div>}
          />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(screen.getByLabelText(/심볼/)).toBeInTheDocument(),
    );

    await userEvent.type(screen.getByLabelText(/심볼/), "KRW-BTC");
    await userEvent.selectOptions(
      screen.getByLabelText(/종목 유형/),
      "crypto",
    );
    await userEvent.click(screen.getByRole("button", { name: /세션 시작/ }));

    await waitFor(() => expect(screen.getByText("detail-77")).toBeInTheDocument());
    const post = calls.find((c) => c.method === "POST");
    expect(post?.url).toContain("/research-pipeline/sessions");
  });
});

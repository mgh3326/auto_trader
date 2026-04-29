import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import SessionDetailPage from "../pages/SessionDetailPage";
import {
  makeAnalyticsResponse,
  makeProposal,
  makeResearchRunMarketBrief,
  makeSessionDetail,
} from "../test/fixtures";
import { mockFetch } from "../test/server";

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={["/sessions/session-1"]}>
      <Routes>
        <Route path="/sessions/:sessionUuid" element={<SessionDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("SessionDetailPage", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows market brief and proposals", async () => {
    mockFetch({
      "/trading/api/decisions/session-1": () =>
        new Response(
          JSON.stringify(
            makeSessionDetail({ market_brief: makeResearchRunMarketBrief() }),
          ),
        ),
      "/trading/api/decisions/session-1/analytics": () =>
        new Response(JSON.stringify(makeAnalyticsResponse())),
    });

    renderDetail();

    expect(await screen.findByText("Market brief")).toBeInTheDocument();
    expect(screen.getByText("BTC")).toBeInTheDocument();
    expect(screen.getByText("ETH")).toBeInTheDocument();
    expect(screen.getByText("SOL")).toBeInTheDocument();
    expect(await screen.findByText("Outcome analytics")).toBeInTheDocument();
    expect(screen.getByText("1.25%")).toBeInTheDocument();
    expect(screen.getByText(/Research run/)).toBeInTheDocument();
    expect(screen.getByText(/Reconciliation summary/)).toBeInTheDocument();
    expect(screen.getByText(/Maintain: 1/)).toBeInTheDocument();
    expect(screen.getByText(/Near fill: 1/)).toBeInTheDocument();
    expect(screen.getByText(/KR broker only: 1/)).toBeInTheDocument();
  });

  it("successful respond refetches and updates row", async () => {
    let detailCalls = 0;
    mockFetch({
      "/trading/api/decisions/session-1": () => {
        detailCalls += 1;
        const proposal =
          detailCalls > 1
            ? makeProposal({
                user_response: "accept",
                responded_at: "2026-04-28T07:00:00Z",
              })
            : makeProposal();
        return new Response(
          JSON.stringify(makeSessionDetail({ proposals: [proposal] })),
        );
      },
      "/trading/api/decisions/session-1/analytics": () =>
        new Response(JSON.stringify(makeAnalyticsResponse())),
      "/trading/api/proposals/proposal-btc/respond": () =>
        new Response(JSON.stringify(makeProposal({ user_response: "accept" }))),
    });

    renderDetail();
    await screen.findByText("BTC");
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));

    await waitFor(() => expect(screen.getAllByText("accept").length).toBeGreaterThan(0));
  });

  it("renders not found on 404", async () => {
    mockFetch({
      "/trading/api/decisions/session-1": () =>
        new Response(JSON.stringify({ detail: "Decision session not found" }), {
          status: 404,
        }),
      "/trading/api/decisions/session-1/analytics": () =>
        new Response(JSON.stringify({ detail: "Session not found" }), {
          status: 404,
        }),
    });

    renderDetail();

    expect(await screen.findByText("Session not found")).toBeInTheDocument();
  });

  it("shows archived banner on 409 respond", async () => {
    mockFetch({
      "/trading/api/decisions/session-1": () =>
        new Response(JSON.stringify(makeSessionDetail({ proposals: [makeProposal()] }))),
      "/trading/api/decisions/session-1/analytics": () =>
        new Response(JSON.stringify(makeAnalyticsResponse())),
      "/trading/api/proposals/proposal-btc/respond": () =>
        new Response(JSON.stringify({ detail: "Session is archived" }), {
          status: 409,
        }),
    });

    renderDetail();
    await screen.findByText("BTC");
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));

    expect(
      await screen.findByText("Session is archived. You can no longer respond."),
    ).toBeInTheDocument();
  });
});

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
  makeStrategyEvent,
  makeStrategyEventListResponse,
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
      "/trading/api/strategy-events?session_uuid=session-1&limit=50&offset=0":
        () =>
          new Response(
            JSON.stringify(makeStrategyEventListResponse({ events: [], total: 0 })),
          ),
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

  it("renders session-scoped strategy events timeline", async () => {
    mockFetch({
      "/trading/api/decisions/session-1": () =>
        new Response(JSON.stringify(makeSessionDetail())),
      "/trading/api/decisions/session-1/analytics": () =>
        new Response(JSON.stringify(makeAnalyticsResponse())),
      "/trading/api/strategy-events?session_uuid=session-1&limit=50&offset=0":
        () =>
          new Response(
            JSON.stringify(
              makeStrategyEventListResponse({
                events: [
                  makeStrategyEvent({
                    source_text: "Fed hike confirmed",
                    affected_symbols: ["TSLA"],
                  }),
                ],
              }),
            ),
          ),
    });

    renderDetail();

    expect(await screen.findByText("Strategy events")).toBeInTheDocument();
    expect(await screen.findByText(/fed hike confirmed/i)).toBeInTheDocument();
    expect(screen.getByText("TSLA")).toBeInTheDocument();
    expect(screen.getByText(/operator_market_event/i)).toBeInTheDocument();
  });

  it("renders an empty state when there are no strategy events", async () => {
    mockFetch({
      "/trading/api/decisions/session-1": () =>
        new Response(JSON.stringify(makeSessionDetail())),
      "/trading/api/decisions/session-1/analytics": () =>
        new Response(JSON.stringify(makeAnalyticsResponse())),
      "/trading/api/strategy-events?session_uuid=session-1&limit=50&offset=0":
        () =>
          new Response(
            JSON.stringify(
              makeStrategyEventListResponse({ events: [], total: 0 }),
            ),
          ),
    });

    renderDetail();

    expect(
      await screen.findByText(/no strategy events yet/i),
    ).toBeInTheDocument();
  });

  it("submitting the operator event form POSTs operator_market_event with current session_uuid and refreshes the timeline", async () => {
    let listCalls = 0;
    const recorded: { url: string; method: string; body?: string }[] = [];
    mockFetch({
      "/trading/api/decisions/session-1": () =>
        new Response(JSON.stringify(makeSessionDetail())),
      "/trading/api/decisions/session-1/analytics": () =>
        new Response(JSON.stringify(makeAnalyticsResponse())),
      "/trading/api/strategy-events?session_uuid=session-1&limit=50&offset=0":
        () => {
          listCalls += 1;
          if (listCalls === 1) {
            return new Response(
              JSON.stringify(
                makeStrategyEventListResponse({ events: [], total: 0 }),
              ),
            );
          }
          return new Response(
            JSON.stringify(
              makeStrategyEventListResponse({
                events: [
                  makeStrategyEvent({
                    source_text: "OpenAI earnings missed",
                    affected_symbols: ["MSFT"],
                  }),
                ],
                total: 1,
              }),
            ),
          );
        },
      "/trading/api/strategy-events": (req) => {
        return req.text().then((body) => {
          recorded.push({ url: req.url, method: req.method, body });
          return new Response(
            JSON.stringify(
              makeStrategyEvent({
                source_text: "OpenAI earnings missed",
                affected_symbols: ["MSFT"],
              }),
            ),
            { status: 201 },
          );
        });
      },
    });

    renderDetail();

    await screen.findByText(/no strategy events yet/i);

    await userEvent.type(
      screen.getByLabelText(/source text/i),
      "OpenAI earnings missed",
    );
    await userEvent.type(
      screen.getByLabelText(/affected symbols/i),
      "MSFT",
    );
    await userEvent.click(
      screen.getByRole("button", { name: /add event/i }),
    );

    await waitFor(() => expect(recorded.length).toBe(1));
    const sentBody = JSON.parse(recorded[0]!.body ?? "{}");
    expect(sentBody.source).toBe("user");
    expect(sentBody.event_type).toBe("operator_market_event");
    expect(sentBody.session_uuid).toBe("session-1");
    expect(sentBody.source_text).toBe("OpenAI earnings missed");
    expect(sentBody.affected_symbols).toEqual(["MSFT"]);

    expect(
      await screen.findByText(/openai earnings missed/i),
    ).toBeInTheDocument();
  });

  it("surfaces a strategy-event submit error without mutating proposals", async () => {
    let proposalRespondCalled = false;
    mockFetch({
      "/trading/api/decisions/session-1": () =>
        new Response(JSON.stringify(makeSessionDetail())),
      "/trading/api/decisions/session-1/analytics": () =>
        new Response(JSON.stringify(makeAnalyticsResponse())),
      "/trading/api/strategy-events?session_uuid=session-1&limit=50&offset=0":
        () =>
          new Response(
            JSON.stringify(
              makeStrategyEventListResponse({ events: [], total: 0 }),
            ),
          ),
      "/trading/api/strategy-events": () =>
        new Response(JSON.stringify({ detail: "validation failed" }), {
          status: 422,
        }),
      "/trading/api/proposals/proposal-btc/respond": () => {
        proposalRespondCalled = true;
        return new Response(JSON.stringify({}));
      },
    });

    renderDetail();

    await screen.findByText(/no strategy events yet/i);
    await userEvent.type(screen.getByLabelText(/source text/i), "msg");
    await userEvent.click(
      screen.getByRole("button", { name: /add event/i }),
    );

    expect(
      await screen.findByText(/validation failed/i),
    ).toBeInTheDocument();
    expect(proposalRespondCalled).toBe(false);
  });
});

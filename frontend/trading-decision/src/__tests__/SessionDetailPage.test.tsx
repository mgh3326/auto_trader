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

    expect(await screen.findByText("시장 브리핑")).toBeInTheDocument();
    expect(screen.getByText("BTC")).toBeInTheDocument();
    expect(screen.getByText("ETH")).toBeInTheDocument();
    expect(screen.getByText("SOL")).toBeInTheDocument();
    expect(await screen.findByText("결과 분석")).toBeInTheDocument();
    expect(screen.getByText("1.25%")).toBeInTheDocument();
    expect(screen.getByText(/리서치 실행/)).toBeInTheDocument();
    expect(screen.getByText(/조정 요약/)).toBeInTheDocument();
    expect(screen.getByText(/유지: 1/)).toBeInTheDocument();
    expect(screen.getByText(/체결 임박: 1/)).toBeInTheDocument();
    expect(screen.getByText(/국내 브로커 전용: 1/)).toBeInTheDocument();
  });

  it("renders structured workflow market brief fields with Korean labels", async () => {
    mockFetch({
      "/trading/api/decisions/session-1": () =>
        new Response(
          JSON.stringify(
            makeSessionDetail({
              notes: null,
              market_brief: {
                title: "BTC paper preview via Upbit signal plus Alpaca Paper execution",
                safety_scope: "preview_only_confirm_false_no_broker_submit",
                purpose: "paper_plumbing_smoke",
                signal_venue: "Upbit",
                signal_symbol: "KRW-BTC",
                execution_venue: "Alpaca Paper",
                execution_symbol: "BTC/USD",
                created_from_prompt: "BTC paper preview 만들어줘",
              },
            }),
          ),
        ),
      "/trading/api/decisions/session-1/analytics": () =>
        new Response(JSON.stringify(makeAnalyticsResponse())),
    });

    renderDetail();

    expect(await screen.findByText("시장 브리핑")).toBeInTheDocument();
    expect(screen.getByText(/브리핑 유형:/)).toBeInTheDocument();
    expect(screen.getByText(/페이퍼 배관 점검/)).toBeInTheDocument();
    expect(screen.getByText(/안전 범위:/)).toBeInTheDocument();
    expect(screen.getByText(/브로커 제출 없는 preview 전용/)).toBeInTheDocument();
    expect(screen.getByText(/신호 기준:/)).toBeInTheDocument();
    expect(screen.getAllByText(/KRW-BTC/).length).toBeGreaterThan(0);
    expect(screen.getByText(/실행 대상:/)).toBeInTheDocument();
    expect(screen.getAllByText(/BTC\/USD/).length).toBeGreaterThan(0);
    expect(screen.getByText("원본 데이터 보기")).toBeInTheDocument();
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
    await userEvent.click(screen.getByRole("button", { name: "수락" }));

    await waitFor(() => expect(screen.getAllByText("수락").length).toBeGreaterThan(0));
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

    expect(await screen.findByText("세션을 찾을 수 없습니다")).toBeInTheDocument();
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
    await userEvent.click(screen.getByRole("button", { name: "수락" }));

    expect(
      await screen.findByText("세션이 보관되었습니다. 더 이상 응답할 수 없습니다."),
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

    expect(await screen.findByText("전략 이벤트")).toBeInTheDocument();
    expect(await screen.findByText(/fed hike confirmed/i)).toBeInTheDocument();
    expect(screen.getByText("TSLA")).toBeInTheDocument();
    expect(screen.getByText("운영자 시장 이벤트")).toBeInTheDocument();
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
      await screen.findByText(/전략 이벤트가 없습니다/),
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

    await screen.findByText(/전략 이벤트가 없습니다/);

    await userEvent.type(
      screen.getByLabelText(/소스 텍스트/),
      "OpenAI earnings missed",
    );
    await userEvent.type(
      screen.getByLabelText(/영향 종목/),
      "MSFT",
    );
    await userEvent.click(
      screen.getByRole("button", { name: /이벤트 추가/ }),
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

    await screen.findByText(/전략 이벤트가 없습니다/);
    await userEvent.type(screen.getByLabelText(/소스 텍스트/), "msg");
    await userEvent.click(
      screen.getByRole("button", { name: /이벤트 추가/ }),
    );

    expect(
      await screen.findByText(/validation failed/i),
    ).toBeInTheDocument();
    expect(proposalRespondCalled).toBe(false);
  });
});

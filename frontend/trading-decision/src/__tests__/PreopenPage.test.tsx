import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import PreopenPage from "../pages/PreopenPage";
import {
  makePreopenFailOpen,
  makePreopenLinkedSession,
  makePreopenBlockedPaperApprovalBridge,
  makePreopenBriefingArtifact,
  makePreopenMarketNewsBriefing,
  makePreopenPaperApprovalBridge,
  makePreopenQaEvaluator,
  makePreopenUnavailableQaEvaluator,
  makePreopenNewsArticle,
  makePreopenNewsStale,
  makePreopenNewsUnavailable,
  makePreopenResponse,
} from "../test/fixtures/preopen";
import { mockFetch } from "../test/server";

const PREOPEN_URL = "/trading/api/preopen/latest?market_scope=kr";
const CREATE_URL = "/trading/api/decisions/from-research-run";

describe("PreopenPage", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("renders loading then fail-open banner with advisory_skipped_reason and no CTA", async () => {
    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(JSON.stringify(makePreopenFailOpen())),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    expect(await screen.findByText(/사용 가능한 장전 리서치 실행 결과가 없습니다/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: /장전 브리핑/i })).toBeInTheDocument();
    expect(screen.getAllByText(/no open preopen run/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/산출물 미사용/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /의사결정 세션 생성/i })).toBeNull();
  });

  it("renders run summary, candidates, reconciliations from fixture", async () => {
    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(JSON.stringify(makePreopenResponse())),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    // Symbol appears in candidates table, reconciliations table, AND basket preview
    expect(await screen.findAllByText("005930")).toHaveLength(3);
    expect(screen.getByText("체결 임박")).toBeInTheDocument();
    expect(screen.getByText(/Morning scan/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: /장전 브리핑/i })).toBeInTheDocument();
    expect(screen.getByText(/산출물 준비 완료/i)).toBeInTheDocument();
    expect(screen.getByText(/preopen_briefing v1/i)).toBeInTheDocument();
    expect(screen.getByText(/뉴스 요약: 장전 핵심 뉴스/i)).toBeInTheDocument();
  });

  it("renders degraded briefing artifact without hiding ROB-75 market news", async () => {
    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(
          JSON.stringify(
            makePreopenResponse({
              briefing_artifact: makePreopenBriefingArtifact({
                status: "degraded",
                risk_notes: ["market_news_briefing_unavailable"],
              }),
              market_news_briefing: makePreopenMarketNewsBriefing(),
            }),
          ),
        ),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    expect(await screen.findByText(/산출물 주의/i)).toBeInTheDocument();
    expect(screen.getByText(/market news briefing unavailable/i)).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: /시장 뉴스 브리핑/i }),
    ).toBeInTheDocument();
  });


  it("renders QA evaluator score, checks, and guardrail copy", async () => {
    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(JSON.stringify(makePreopenResponse())),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    expect(
      await screen.findByRole("region", { name: /장전 QA 평가기/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/QA 준비 완료/i)).toBeInTheDocument();
    expect(screen.getByText(/종합 점수: 90/i)).toBeInTheDocument();
    expect(screen.getByText(/Actionability guardrail/i)).toBeInTheDocument();
    expect(screen.getByText(/execution remains disabled/i)).toBeInTheDocument();
  });

  it("renders QA evaluator needs-review operator labels", async () => {
    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(
          JSON.stringify(
            makePreopenResponse({
              qa_evaluator: makePreopenQaEvaluator({
                status: "needs_review",
                overall: {
                  score: 70,
                  grade: "watch",
                  confidence: "medium",
                  reason: "news stale",
                },
                blocking_reasons: ["news_readiness"],
                warnings: ["News readiness is stale; review before relying on recommendations."],
                checks: [
                  {
                    id: "news_readiness",
                    label: "News readiness",
                    status: "warn",
                    severity: "medium",
                    summary: "News readiness needs review before relying on recommendations.",
                    details: null,
                  },
                ],
              }),
            }),
          ),
        ),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    expect(await screen.findByText(/QA 검토 필요/i)).toBeInTheDocument();
    expect(screen.queryByText(/QA needs_review/i)).toBeNull();
    expect(
      screen.getAllByText(/News readiness needs review before relying on recommendations/i).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText("news_readiness")).toBeNull();
  });

  it("renders unavailable QA evaluator with human-readable blocking reason", async () => {
    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(
          JSON.stringify(
            makePreopenResponse({
              qa_evaluator: makePreopenUnavailableQaEvaluator(),
            }),
          ),
        ),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    expect(await screen.findByText(/QA 미사용/i)).toBeInTheDocument();
    expect(
      screen.getAllByText(/No open preopen research run is available/i).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText("no_open_preopen_run")).toBeNull();
  });

  it("clicking 의사결정 세션 생성 calls api with correct args and navigates", async () => {
    const user = userEvent.setup();
    const sessionUuid = "sess-aaaa-1111-2222-333333333333";

    const { calls } = mockFetch({
      [PREOPEN_URL]: () =>
        new Response(JSON.stringify(makePreopenResponse())),
      [CREATE_URL]: () =>
        new Response(
          JSON.stringify({
            session_uuid: sessionUuid,
            session_url: `/trading/decisions/sessions/${sessionUuid}`,
            status: "open",
            advisory_skipped_reason: null,
            warnings: [],
          }),
          { status: 201 },
        ),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    // Wait for page to load. Label "Create decision session" comes from makePreopenBriefingArtifact fixture.
    const createBtn = await screen.findByRole("button", { name: /Create decision session/i });
    expect(createBtn).toBeInTheDocument();

    // First click triggers confirm prompt
    await user.click(createBtn);
    expect(screen.getByRole("button", { name: /의사결정 세션을 생성하시겠습니까/i })).toBeInTheDocument();

    // Second click (confirm) submits
    await user.click(screen.getByRole("button", { name: /의사결정 세션을 생성하시겠습니까/i }));

    await waitFor(() => {
      const postCall = calls.find((c) => c.method === "POST");
      expect(postCall).toBeDefined();
      const body = JSON.parse(postCall?.body ?? "{}");
      expect(body.selector.run_uuid).toBe("run-1111-2222-3333-444444444444");
      expect(body.include_tradingagents).toBe(false);
      expect(body.notes).toBe("Created from preopen dashboard");
    });
  });

  it("hides 의사결정 세션 생성 when a linked session already exists", async () => {
    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(
          JSON.stringify(
            makePreopenResponse({
              linked_sessions: [makePreopenLinkedSession()],
            }),
          ),
        ),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    expect(await screen.findByRole("link", { name: /세션 열기/i })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Create decision session/i }),
    ).toBeNull();
  });

  it("renders 정상 badge with source counts and a news preview link", async () => {
    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(
          JSON.stringify(
            makePreopenResponse({
              news_preview: [
                makePreopenNewsArticle({
                  id: 9001,
                  title: "삼성전자 영업이익",
                  url: "https://example.com/9001",
                }),
              ],
            }),
          ),
        ),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    expect(await screen.findByTestId("news-readiness-section")).toBeInTheDocument();
    expect(screen.getByText("정상")).toBeInTheDocument();
    expect(screen.getByText(/mk_stock: 12/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /소스 커버리지/i })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "mk_stock" })).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /삼성전자 영업이익/ }),
    ).toHaveAttribute("href", "https://example.com/9001");
  });

  it("renders 오래됨 badge with explicit warning text", async () => {
    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(
          JSON.stringify(
            makePreopenResponse({
              news: makePreopenNewsStale(),
            }),
          ),
        ),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    expect(await screen.findByText("오래됨")).toBeInTheDocument();
    expect(
      screen.getByText(/뉴스가 180분 이상 경과했습니다/i),
    ).toBeInTheDocument();
  });

  it("renders 미사용 badge when news section reports no data", async () => {
    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(
          JSON.stringify(
            makePreopenResponse({
              news: makePreopenNewsUnavailable(),
              news_preview: [],
            }),
          ),
        ),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    expect(await screen.findByText("미사용")).toBeInTheDocument();
    expect(
      screen.getByText(/미리 볼 최근 기사가 없습니다/i),
    ).toBeInTheDocument();
  });

  it("renders 미사용 badge with degraded message when news is null", async () => {
    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(
          JSON.stringify(
            makePreopenResponse({
              news: null,
              news_preview: [],
            }),
          ),
        ),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    expect(await screen.findByText("미사용")).toBeInTheDocument();
    expect(
      screen.getByText(/뉴스 준비도 조회에 실패했습니다/i),
    ).toBeInTheDocument();
  });

  it("renders market news briefing sections and filtered count", async () => {
    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(
          JSON.stringify(
            makePreopenResponse({
              market_news_briefing: makePreopenMarketNewsBriefing(),
            }),
          ),
        ),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    expect(
      await screen.findByRole("region", { name: /시장 뉴스 브리핑/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Preopen headlines/i)).toBeInTheDocument();
    expect(screen.getByText(/필터링된 노이즈: 2/i)).toBeInTheDocument();
    expect(screen.getByText(/점수 82/i)).toBeInTheDocument();
    expect(screen.getByText(/매칭 키워드: AI, 반도체/i)).toBeInTheDocument();
  });

  it("renders market news briefing fail-open state when field is null", async () => {
    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(
          JSON.stringify(
            makePreopenResponse({
              market_news_briefing: null,
            }),
          ),
        ),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    expect(
      await screen.findByRole("region", { name: /시장 뉴스 브리핑/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/아직 시장 뉴스 브리핑이 없습니다/i),
    ).toBeInTheDocument();
  });

  it("renders paper approval preview with safety copy and venue provenance", async () => {
    const { calls } = mockFetch({
      [PREOPEN_URL]: () =>
        new Response(
          JSON.stringify(
            makePreopenResponse({
              market_scope: "crypto",
              paper_approval_bridge: makePreopenPaperApprovalBridge(),
            }),
          ),
        ),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    expect(
      await screen.findByRole("region", { name: /모의 승인 프리뷰/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/프리뷰 사용 가능/i)).toBeInTheDocument();
    expect(screen.getByText(/자문 전용 프리뷰/i)).toBeInTheDocument();
    expect(
      screen.getByText(/이 화면에서 실행할 수 없습니다/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Alpaca Paper 제출 전에 트레이더의 명시적 승인이 필요합니다/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/이 카드는 모의 주문을 제출하거나 취소하지 않습니다/i),
    ).toBeInTheDocument();
    expect(screen.getByText("시그널 소스")).toBeInTheDocument();
    expect(screen.getAllByText(/Upbit KRW-BTC/i).length).toBeGreaterThan(0);
    expect(screen.getByText("실행 거래소")).toBeInTheDocument();
    expect(screen.getAllByText(/Alpaca Paper BTC\/USD/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/프리뷰 페이로드: 매수 limit · \$10 @ 1.00 GTC/i)).toBeInTheDocument();
    expect(calls).toHaveLength(1);
    expect(calls[0]?.method).toBe("GET");
  });

  it("renders blocked paper approval preview without execution actions", async () => {
    const { calls } = mockFetch({
      [PREOPEN_URL]: () =>
        new Response(
          JSON.stringify(
            makePreopenResponse({
              paper_approval_bridge: makePreopenBlockedPaperApprovalBridge(),
            }),
          ),
        ),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    expect(
      await screen.findByRole("region", { name: /모의 승인 프리뷰/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/프리뷰 차단됨/i)).toBeInTheDocument();
    expect(screen.getByText(/qa evaluator unavailable/i)).toBeInTheDocument();
    expect(
      screen.getByText(/현재 사용 가능한 모의 승인 프리뷰 후보가 없습니다/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /submit|cancel paper|place order/i })).toBeNull();
    expect(calls).toHaveLength(1);
  });

  it("surfaces ApiError detail (research_run_has_no_candidates) inline without throwing", async () => {
    const user = userEvent.setup();

    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(JSON.stringify(makePreopenResponse())),
      [CREATE_URL]: () =>
        new Response(
          JSON.stringify({ detail: "research_run_has_no_candidates" }),
          { status: 422 },
        ),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });
    const createBtn = await screen.findByRole("button", { name: /Create decision session/i });

    // First click → confirm
    await user.click(createBtn);
    // Second click → submit
    await user.click(screen.getByRole("button", { name: /의사결정 세션을 생성하시겠습니까/i }));

    expect(
      await screen.findByText(/research_run_has_no_candidates/i),
    ).toBeInTheDocument();
  });
});

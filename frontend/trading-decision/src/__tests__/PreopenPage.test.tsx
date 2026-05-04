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

    expect(await screen.findByText(/No preopen research run available/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: /Preopen briefing/i })).toBeInTheDocument();
    expect(screen.getAllByText(/no_open_preopen_run/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/Artifact unavailable/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /create decision session/i })).toBeNull();
  });

  it("renders run summary, candidates, reconciliations from fixture", async () => {
    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(JSON.stringify(makePreopenResponse())),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    // Symbol appears in both candidates and reconciliations tables
    expect(await screen.findAllByText("005930")).toHaveLength(2);
    expect(screen.getByText("near_fill")).toBeInTheDocument();
    expect(screen.getByText(/Morning scan/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: /Preopen briefing/i })).toBeInTheDocument();
    expect(screen.getByText(/Artifact ready/i)).toBeInTheDocument();
    expect(screen.getByText(/preopen_briefing v1/i)).toBeInTheDocument();
    expect(screen.getByText(/News brief: 장전 핵심 뉴스/i)).toBeInTheDocument();
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

    expect(await screen.findByText(/Artifact degraded/i)).toBeInTheDocument();
    expect(screen.getByText(/market_news_briefing_unavailable/i)).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: /market news briefing/i }),
    ).toBeInTheDocument();
  });


  it("renders QA evaluator score, checks, and guardrail copy", async () => {
    mockFetch({
      [PREOPEN_URL]: () =>
        new Response(JSON.stringify(makePreopenResponse())),
    });

    render(<PreopenPage />, { wrapper: MemoryRouter });

    expect(
      await screen.findByRole("region", { name: /preopen qa evaluator/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/QA ready/i)).toBeInTheDocument();
    expect(screen.getByText(/Overall score: 90/i)).toBeInTheDocument();
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

    expect(await screen.findByText(/QA needs review/i)).toBeInTheDocument();
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

    expect(await screen.findByText(/QA unavailable/i)).toBeInTheDocument();
    expect(
      screen.getAllByText(/No open preopen research run is available/i).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText("no_open_preopen_run")).toBeNull();
  });

  it("clicking Create decision session calls api with correct args and navigates", async () => {
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

    // Wait for page to load
    expect(await screen.findByRole("button", { name: /create decision session/i })).toBeInTheDocument();

    // First click triggers confirm prompt
    await user.click(screen.getByRole("button", { name: /create decision session/i }));
    expect(screen.getByRole("button", { name: /confirm/i })).toBeInTheDocument();

    // Second click (confirm) submits
    await user.click(screen.getByRole("button", { name: /confirm/i }));

    await waitFor(() => {
      const postCall = calls.find((c) => c.method === "POST");
      expect(postCall).toBeDefined();
      const body = JSON.parse(postCall?.body ?? "{}");
      expect(body.selector.run_uuid).toBe("run-1111-2222-3333-444444444444");
      expect(body.include_tradingagents).toBe(false);
      expect(body.notes).toBe("Created from preopen dashboard");
    });
  });

  it("hides Create decision session when a linked session already exists", async () => {
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

    expect(await screen.findByRole("link", { name: /open session/i })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /create decision session/i }),
    ).toBeNull();
  });

  it("renders Ready badge with source counts and a news preview link", async () => {
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
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText(/mk_stock: 12/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /source coverage/i })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "mk_stock" })).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /삼성전자 영업이익/ }),
    ).toHaveAttribute("href", "https://example.com/9001");
  });

  it("renders Stale badge with explicit warning text", async () => {
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

    expect(await screen.findByText("Stale")).toBeInTheDocument();
    expect(
      screen.getByText(/News is older than 180 min/i),
    ).toBeInTheDocument();
  });

  it("renders Unavailable badge when news section reports no data", async () => {
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

    expect(await screen.findByText("Unavailable")).toBeInTheDocument();
    expect(
      screen.getByText(/No recent articles to preview/i),
    ).toBeInTheDocument();
  });

  it("renders Unavailable badge with degraded message when news is null", async () => {
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

    expect(await screen.findByText("Unavailable")).toBeInTheDocument();
    expect(
      screen.getByText(/News readiness lookup failed/i),
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
      await screen.findByRole("region", { name: /market news briefing/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Preopen headlines/i)).toBeInTheDocument();
    expect(screen.getByText(/Filtered noise: 2/i)).toBeInTheDocument();
    expect(screen.getByText(/Score 82/i)).toBeInTheDocument();
    expect(screen.getByText(/Terms: AI, 반도체/i)).toBeInTheDocument();
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
      await screen.findByRole("region", { name: /market news briefing/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/No market news briefing available yet/i),
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
      await screen.findByRole("region", { name: /paper approval preview/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Preview Available/i)).toBeInTheDocument();
    expect(screen.getByText(/Advisory-only preview/i)).toBeInTheDocument();
    expect(
      screen.getByText(/Execution is not allowed from this screen/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Explicit operator approval is required before any Alpaca Paper submit/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/does not submit or cancel paper orders/i),
    ).toBeInTheDocument();
    expect(screen.getByText("Signal source")).toBeInTheDocument();
    expect(screen.getAllByText(/Upbit KRW-BTC/i).length).toBeGreaterThan(0);
    expect(screen.getByText("Execution venue")).toBeInTheDocument();
    expect(screen.getAllByText(/Alpaca Paper BTC\/USD/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/Preview payload: buy limit · \$10 @ 1.00 GTC/i)).toBeInTheDocument();
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
      await screen.findByRole("region", { name: /paper approval preview/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Preview Blocked/i)).toBeInTheDocument();
    expect(screen.getByText(/qa evaluator unavailable/i)).toBeInTheDocument();
    expect(
      screen.getByText(/No paper approval preview candidates are currently available/i),
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
    await screen.findByRole("button", { name: /create decision session/i });

    // First click → confirm
    await user.click(screen.getByRole("button", { name: /create decision session/i }));
    // Second click → submit
    await user.click(screen.getByRole("button", { name: /confirm/i }));

    expect(
      await screen.findByText(/research_run_has_no_candidates/i),
    ).toBeInTheDocument();
  });
});

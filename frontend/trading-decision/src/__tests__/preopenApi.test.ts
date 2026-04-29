import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createDecisionFromResearchRun,
  getLatestPreopen,
} from "../api/preopen";
import {
  makePreopenFailOpen,
  makePreopenResponse,
} from "../test/fixtures/preopen";
import { mockFetch } from "../test/server";

describe("preopen API client", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("getLatestPreopen builds correct path with market_scope=kr", async () => {
    const { calls } = mockFetch({
      "/trading/api/preopen/latest?market_scope=kr": () =>
        new Response(JSON.stringify(makePreopenFailOpen())),
    });

    await getLatestPreopen("kr");

    expect(calls[0]?.url).toContain("/trading/api/preopen/latest?market_scope=kr");
    expect(calls[0]?.method).toBe("GET");
  });

  it("getLatestPreopen returns parsed response", async () => {
    mockFetch({
      "/trading/api/preopen/latest?market_scope=kr": () =>
        new Response(JSON.stringify(makePreopenResponse())),
    });

    const data = await getLatestPreopen();

    expect(data.has_run).toBe(true);
    expect(data.candidate_count).toBe(1);
    expect(data.candidates[0]?.symbol).toBe("005930");
  });

  it("createDecisionFromResearchRun POSTs correct body", async () => {
    const runUuid = "run-1111-2222-3333-444444444444";
    const { calls } = mockFetch({
      "/trading/api/decisions/from-research-run": () =>
        new Response(
          JSON.stringify({
            session_uuid: "sess-uuid",
            session_url: "/trading/decisions/sessions/sess-uuid",
            status: "open",
            advisory_skipped_reason: null,
            warnings: [],
          }),
          { status: 201 },
        ),
    });

    const result = await createDecisionFromResearchRun({ runUuid });

    expect(result.session_uuid).toBe("sess-uuid");
    expect(calls[0]?.method).toBe("POST");
    const body = JSON.parse(calls[0]?.body ?? "{}");
    expect(body.selector.run_uuid).toBe(runUuid);
    expect(body.include_tradingagents).toBe(false);
    expect(body.notes).toBe("Created from preopen dashboard");
  });

  it("only calls /trading/api paths", async () => {
    const { calls } = mockFetch({
      "/trading/api/preopen/latest?market_scope=kr": () =>
        new Response(JSON.stringify(makePreopenFailOpen())),
    });

    await getLatestPreopen("kr");

    for (const call of calls) {
      expect(new URL(call.url, "https://example.test").pathname).toMatch(
        /^\/trading\/api\//,
      );
    }
  });
});

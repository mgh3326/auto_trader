import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createSession,
  getSession,
  getSessionFull,
  getSessionStages,
  getSessionSummary,
  getSymbolTimeline,
  listSessions,
} from "../api/researchPipeline";
import {
  makeCreateResponse,
  makeSessionFull,
  makeSessionListItem,
  makeSymbolTimeline,
} from "../test/fixtures/research";
import { mockFetch } from "../test/server";

describe("researchPipeline API client", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("listSessions hits /api/research-pipeline/sessions with limit", async () => {
    const { calls } = mockFetch({
      "/trading/api/research-pipeline/sessions?limit=20": () =>
        new Response(JSON.stringify([makeSessionListItem()])),
    });
    const data = await listSessions({ limit: 20 });
    expect(data).toHaveLength(1);
    expect(calls[0]?.method).toBe("GET");
  });

  it("createSession POSTs body and returns session_id", async () => {
    const { calls } = mockFetch({
      "/trading/api/research-pipeline/sessions": () =>
        new Response(JSON.stringify(makeCreateResponse({ session_id: 42 })), {
          status: 201,
        }),
    });
    const result = await createSession({
      symbol: "KRW-BTC",
      instrument_type: "crypto",
    });
    expect(result.session_id).toBe(42);
    expect(calls[0]?.method).toBe("POST");
    expect(JSON.parse(calls[0]?.body ?? "{}")).toEqual({
      symbol: "KRW-BTC",
      instrument_type: "crypto",
      triggered_by: "user",
    });
  });

  it("getSession hits /sessions/:id", async () => {
    mockFetch({
      "/trading/api/research-pipeline/sessions/1": () =>
        new Response(JSON.stringify({ id: 1, status: "running" })),
    });
    const data = await getSession(1);
    expect(data.id).toBe(1);
  });

  it("getSessionFull hits /sessions/:id?include=full", async () => {
    const { calls } = mockFetch({
      "/trading/api/research-pipeline/sessions/1?include=full": () =>
        new Response(JSON.stringify(makeSessionFull())),
    });
    const data = await getSessionFull(1);
    expect(data.session.id).toBe(1);
    expect(data.stages).toHaveLength(4);
    expect(calls[0]?.url).toContain("include=full");
  });

  it("getSessionStages hits /sessions/:id/stages", async () => {
    mockFetch({
      "/trading/api/research-pipeline/sessions/1/stages": () =>
        new Response(JSON.stringify([])),
    });
    const data = await getSessionStages(1);
    expect(Array.isArray(data)).toBe(true);
  });

  it("getSessionSummary hits /sessions/:id/summary", async () => {
    mockFetch({
      "/trading/api/research-pipeline/sessions/1/summary": () =>
        new Response(JSON.stringify({ id: 1, decision: "buy", confidence: 80 })),
    });
    const data = await getSessionSummary(1);
    expect(data.decision).toBe("buy");
  });

  it("getSymbolTimeline hits /symbols/:symbol/timeline?days=30", async () => {
    const { calls } = mockFetch({
      "/trading/api/research-pipeline/symbols/AAPL/timeline?days=30": () =>
        new Response(JSON.stringify(makeSymbolTimeline())),
    });
    const data = await getSymbolTimeline("AAPL", 30);
    expect(data.symbol).toBe("AAPL");
    expect(calls[0]?.url).toContain("days=30");
  });
});

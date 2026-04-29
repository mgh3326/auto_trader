import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createStrategyEvent,
  getStrategyEvents,
} from "../api/strategyEvents";
import { mockFetch } from "../test/server";

describe("strategyEvents API client", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("getStrategyEvents builds query string with session_uuid", async () => {
    const { calls } = mockFetch({
      "/trading/api/strategy-events?session_uuid=session-1&limit=50&offset=0":
        () =>
          new Response(
            JSON.stringify({
              events: [],
              total: 0,
              limit: 50,
              offset: 0,
            }),
          ),
    });

    const data = await getStrategyEvents({ sessionUuid: "session-1" });

    expect(data.total).toBe(0);
    expect(calls[0]?.method).toBe("GET");
    expect(calls[0]?.url).toContain(
      "/trading/api/strategy-events?session_uuid=session-1&limit=50&offset=0",
    );
  });

  it("getStrategyEvents passes custom limit/offset", async () => {
    const { calls } = mockFetch({
      "/trading/api/strategy-events?session_uuid=session-1&limit=25&offset=10":
        () =>
          new Response(
            JSON.stringify({
              events: [],
              total: 0,
              limit: 25,
              offset: 10,
            }),
          ),
    });

    await getStrategyEvents({
      sessionUuid: "session-1",
      limit: 25,
      offset: 10,
    });

    expect(calls[0]?.url).toContain("limit=25&offset=10");
  });

  it("createStrategyEvent POSTs body and parses StrategyEventDetail", async () => {
    const { calls } = mockFetch({
      "/trading/api/strategy-events": () =>
        new Response(
          JSON.stringify({
            id: 1,
            event_uuid: "ev-1",
            session_uuid: "session-1",
            source: "user",
            event_type: "operator_market_event",
            source_text: "OpenAI earnings miss",
            normalized_summary: null,
            affected_markets: [],
            affected_sectors: [],
            affected_themes: [],
            affected_symbols: ["MSFT", "NVDA"],
            severity: 3,
            confidence: 60,
            created_by_user_id: 7,
            metadata: null,
            created_at: "2026-04-29T01:00:00Z",
          }),
          { status: 201 },
        ),
    });

    const result = await createStrategyEvent({
      source: "user",
      event_type: "operator_market_event",
      source_text: "OpenAI earnings miss",
      session_uuid: "session-1",
      affected_symbols: ["MSFT", "NVDA"],
      severity: 3,
      confidence: 60,
    });

    expect(result.event_uuid).toBe("ev-1");
    expect(result.source).toBe("user");
    expect(result.event_type).toBe("operator_market_event");
    expect(calls[0]?.method).toBe("POST");
    const body = JSON.parse(calls[0]?.body ?? "{}");
    expect(body.source).toBe("user");
    expect(body.event_type).toBe("operator_market_event");
    expect(body.session_uuid).toBe("session-1");
    expect(body.source_text).toBe("OpenAI earnings miss");
    expect(body.affected_symbols).toEqual(["MSFT", "NVDA"]);
    expect(body.severity).toBe(3);
    expect(body.confidence).toBe(60);
  });

  it("only calls /trading/api paths", async () => {
    const { calls } = mockFetch({
      "/trading/api/strategy-events?session_uuid=session-1&limit=50&offset=0":
        () =>
          new Response(
            JSON.stringify({
              events: [],
              total: 0,
              limit: 50,
              offset: 0,
            }),
          ),
    });

    await getStrategyEvents({ sessionUuid: "session-1" });

    for (const call of calls) {
      expect(new URL(call.url, "https://example.test").pathname).toMatch(
        /^\/trading\/api\//,
      );
    }
  });
});

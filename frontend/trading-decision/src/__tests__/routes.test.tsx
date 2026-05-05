import { matchRoutes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { isTradingDecisionSessionUuid, tradingDecisionRoutes } from "../routes";

const SESSION_UUID = "11111111-1111-4111-8111-111111111111";

describe("trading decision routes", () => {
  it("keeps canonical session detail route", () => {
    const matches = matchRoutes(tradingDecisionRoutes, `/sessions/${SESSION_UUID}`);

    expect(matches?.at(-1)?.route.path).toBe("/sessions/:sessionUuid");
    expect(matches?.at(-1)?.params.sessionUuid).toBe(SESSION_UUID);
  });

  it("supports legacy generated UUID session URLs as detail aliases", () => {
    const matches = matchRoutes(tradingDecisionRoutes, `/${SESSION_UUID}`);

    expect(matches?.at(-1)?.route.path).toBe("/:sessionUuid");
    expect(matches?.at(-1)?.params.sessionUuid).toBe(SESSION_UUID);
    expect(isTradingDecisionSessionUuid(matches?.at(-1)?.params.sessionUuid)).toBe(true);
  });

  it("does not treat arbitrary single-segment paths as legacy session UUIDs", () => {
    expect(isTradingDecisionSessionUuid("settings")).toBe(false);
    expect(isTradingDecisionSessionUuid("session-1")).toBe(false);
  });

  it("registers the news-radar route", () => {
    const matches = matchRoutes(tradingDecisionRoutes, "/news-radar");
    expect(matches?.at(-1)?.route.path).toBe("/news-radar");
  });

  it("registers /research home route", () => {
    const matches = matchRoutes(tradingDecisionRoutes, "/research");
    expect(matches?.at(-1)?.route.path).toBe("/research");
  });

  it("registers /research/sessions/:sessionId/summary stage route", () => {
    const matches = matchRoutes(
      tradingDecisionRoutes,
      "/research/sessions/42/summary",
    );
    expect(matches?.at(-1)?.route.path).toBe("summary");
    expect(matches?.at(-2)?.route.path).toBe("/research/sessions/:sessionId");
    expect(matches?.at(-2)?.params.sessionId).toBe("42");
  });

  it("registers /research/sessions/:sessionId/market stage route", () => {
    const matches = matchRoutes(
      tradingDecisionRoutes,
      "/research/sessions/42/market",
    );
    expect(matches?.at(-1)?.route.path).toBe("market");
  });

  it("registers /research/sessions/:sessionId/news stage route", () => {
    const matches = matchRoutes(
      tradingDecisionRoutes,
      "/research/sessions/42/news",
    );
    expect(matches?.at(-1)?.route.path).toBe("news");
  });

  it("registers /research/sessions/:sessionId/fundamentals stage route", () => {
    const matches = matchRoutes(
      tradingDecisionRoutes,
      "/research/sessions/42/fundamentals",
    );
    expect(matches?.at(-1)?.route.path).toBe("fundamentals");
  });

  it("does not register /research/sessions/:sessionId/social as a stage route", () => {
    const matches = matchRoutes(
      tradingDecisionRoutes,
      "/research/sessions/42/social",
    );
    // Falls through to the wildcard not-found child instead of a 'social' path.
    expect(matches?.at(-1)?.route.path).toBe("*");
    expect(matches?.at(0)?.route.path).toBe("/research/sessions/:sessionId");
  });

  it("legacy /research/sessions/:sessionId still matches the layout", () => {
    const matches = matchRoutes(
      tradingDecisionRoutes,
      "/research/sessions/42",
    );
    // layout match + index child
    expect(matches?.at(0)?.route.path).toBe("/research/sessions/:sessionId");
    expect(matches?.at(-1)?.route.index).toBe(true);
  });

  it("falls back to a stage-not-found child for unknown segments under a session", () => {
    const matches = matchRoutes(
      tradingDecisionRoutes,
      "/research/sessions/42/bogus",
    );
    expect(matches?.at(-1)?.route.path).toBe("*");
    expect(matches?.at(0)?.route.path).toBe("/research/sessions/:sessionId");
  });

  it("registers /research/symbols/:symbol/timeline route", () => {
    const matches = matchRoutes(
      tradingDecisionRoutes,
      "/research/symbols/AAPL/timeline",
    );
    expect(matches?.at(-1)?.route.path).toBe(
      "/research/symbols/:symbol/timeline",
    );
    expect(matches?.at(-1)?.params.symbol).toBe("AAPL");
  });
});

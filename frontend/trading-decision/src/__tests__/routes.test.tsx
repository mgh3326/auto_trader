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
});

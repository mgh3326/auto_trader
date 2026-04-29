import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import {
  createOutcomeMark,
  getDecisions,
  getSession,
  getSessionAnalytics,
  respondToProposal,
} from "../api/decisions";
import { mockFetch } from "../test/server";

describe("decisions API client", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("getDecisions builds query string", async () => {
    const { calls } = mockFetch({
      "/trading/api/decisions?limit=25&offset=50&status=open": () =>
        new Response(
          JSON.stringify({ sessions: [], total: 0, limit: 25, offset: 50 }),
        ),
    });

    await getDecisions({ limit: 25, offset: 50, status: "open" });

    expect(calls[0]?.url).toContain("limit=25&offset=50&status=open");
  });

  it("getSession hits /decisions/{uuid}", async () => {
    mockFetch({
      "/trading/api/decisions/abc-123": () =>
        new Response(
          JSON.stringify({ session_uuid: "abc-123", proposals: [] }),
        ),
    });

    const data = await getSession("abc-123");

    expect(data.session_uuid).toBe("abc-123");
  });

  it("respondToProposal POSTs body and parses ProposalDetail", async () => {
    const { calls } = mockFetch({
      "/trading/api/proposals/p-1/respond": () =>
        new Response(
          JSON.stringify({ proposal_uuid: "p-1", user_response: "modify" }),
        ),
    });

    const result = await respondToProposal("p-1", {
      response: "modify",
      user_quantity_pct: "10",
    });

    expect(result.user_response).toBe("modify");
    expect(calls[0]?.method).toBe("POST");
    expect(calls[0]?.body).toBe(
      JSON.stringify({ response: "modify", user_quantity_pct: "10" }),
    );
  });

  it("getSessionAnalytics calls GET /trading/api/decisions/:uuid/analytics", async () => {
    const { calls } = mockFetch({
      "/trading/api/decisions/sess-1/analytics": () =>
        new Response(
          JSON.stringify({
            session_uuid: "sess-1",
            generated_at: "2026-04-28T06:00:00Z",
            tracks: [
              "accepted_live",
              "accepted_paper",
              "rejected_counterfactual",
              "analyst_alternative",
              "user_alternative",
            ],
            horizons: ["1h", "4h", "1d", "3d", "7d", "final"],
            cells: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });

    const res = await getSessionAnalytics("sess-1");

    expect(res.session_uuid).toBe("sess-1");
    expect(calls).toHaveLength(1);
    expect(calls[0]?.method).toBe("GET");
  });

  it("createOutcomeMark POSTs to /trading/api/proposals/:uuid/outcomes with the body", async () => {
    const { calls } = mockFetch({
      "/trading/api/proposals/p-1/outcomes": () =>
        new Response(
          JSON.stringify({
            id: 1,
            counterfactual_id: null,
            track_kind: "accepted_live",
            horizon: "1h",
            price_at_mark: "100",
            pnl_pct: null,
            pnl_amount: null,
            marked_at: "2026-04-28T07:00:00Z",
            payload: null,
            created_at: "2026-04-28T07:00:00Z",
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        ),
    });

    const out = await createOutcomeMark("p-1", {
      track_kind: "accepted_live",
      horizon: "1h",
      price_at_mark: "100",
      marked_at: "2026-04-28T07:00:00Z",
    });

    expect(out.track_kind).toBe("accepted_live");
    expect(calls[0]?.method).toBe("POST");
    expect(calls[0]?.body).toContain('"track_kind":"accepted_live"');
  });

  it("401 throws ApiError", async () => {
    mockFetch({
      "/trading/api/decisions": () =>
        new Response(JSON.stringify({ detail: "auth required" }), {
          status: 401,
        }),
    });

    await expect(getDecisions({ limit: 50, offset: 0 })).rejects.toBeInstanceOf(
      ApiError,
    );
    await expect(getDecisions({ limit: 50, offset: 0 })).rejects.toMatchObject({
      status: 401,
    });
  });

  it("422 surfaces detail string", async () => {
    mockFetch({
      "/trading/api/proposals/p-1/respond": () =>
        new Response(
          JSON.stringify({
            detail:
              "modify/partial_accept requires at least one user_* numeric field",
          }),
          { status: 422 },
        ),
    });

    await expect(
      respondToProposal("p-1", { response: "modify" }),
    ).rejects.toMatchObject({
      status: 422,
      detail:
        "modify/partial_accept requires at least one user_* numeric field",
    });
  });

  it("only calls /trading/api paths", async () => {
    const { calls } = mockFetch({
      "/trading/api/decisions": () =>
        new Response(
          JSON.stringify({ sessions: [], total: 0, limit: 50, offset: 0 }),
        ),
    });

    await getDecisions({ limit: 50, offset: 0 });

    for (const call of calls) {
      expect(new URL(call.url, "https://example.test").pathname).toMatch(
        /^\/trading\/api\//,
      );
    }
  });

  it("built bundle contains no forbidden runtime tokens when requested", async () => {
    if (process.env.RUN_BUNDLE_GREP !== "1") return;
    const { readdir, readFile } = await import("node:fs/promises");
    const { join } = await import("node:path");
    const assetsDir = join(process.cwd(), "dist", "assets");
    const files = (await readdir(assetsDir)).filter((file) =>
      file.endsWith(".js"),
    );
    // Keep backend/runtime integration symbols out of the SPA bundle while
    // allowing user-facing copy to use ordinary words such as "broker".
    const forbidden: RegExp[] = [
      /kis\./i,
      /upbit\./i,
      /redis/i,
      /telegram/i,
      /broker[._]/i,
      /order_service/i,
      /fill_notification/i,
      /execution_event/i,
      /watch_alert_service/i,
    ];
    for (const file of files) {
      const body = await readFile(join(assetsDir, file), "utf8");
      for (const token of forbidden) {
        expect(token.test(body)).toBe(false);
      }
    }
  });
});

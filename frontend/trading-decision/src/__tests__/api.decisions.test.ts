import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import {
  getDecisions,
  getSession,
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
      expect(new URL(call.url, "http://x").pathname).toMatch(
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
    const forbidden = [
      "kis.",
      "upbit.",
      "redis",
      "telegram",
      "broker",
      "order_service",
      "fill_notification",
      "execution_event",
      "watch_alert_service",
    ];
    for (const file of files) {
      const body = (await readFile(join(assetsDir, file), "utf8")).toLowerCase();
      for (const token of forbidden) {
        expect(body.includes(token)).toBe(false);
      }
    }
  });
});

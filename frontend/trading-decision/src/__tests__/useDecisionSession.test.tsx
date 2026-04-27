import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useDecisionSession } from "../hooks/useDecisionSession";
import { makeOutcome, makeSessionDetail } from "../test/fixtures";
import { mockFetch } from "../test/server";

describe("useDecisionSession", () => {
  it("recordOutcome posts a mark and refetches the session", async () => {
    const { calls } = mockFetch({
      "/trading/api/decisions/session-1": () =>
        new Response(JSON.stringify(makeSessionDetail())),
      "/trading/api/proposals/proposal-btc/outcomes": () =>
        new Response(JSON.stringify(makeOutcome()), { status: 201 }),
    });

    const { result } = renderHook(() => useDecisionSession("session-1"));
    await waitFor(() => expect(result.current.status).toBe("success"));

    await act(async () => {
      const res = await result.current.recordOutcome("proposal-btc", {
        track_kind: "accepted_live",
        horizon: "1h",
        price_at_mark: "100",
        marked_at: "2026-04-28T07:00:00Z",
      });
      expect(res.ok).toBe(true);
    });

    await waitFor(() =>
      expect(
        calls.filter((call) =>
          call.url.endsWith("/trading/api/decisions/session-1"),
        ),
      ).toHaveLength(2),
    );
    expect(
      calls.some(
        (call) =>
          call.method === "POST" &&
          call.url.endsWith("/trading/api/proposals/proposal-btc/outcomes"),
      ),
    ).toBe(true);

    vi.unstubAllGlobals();
  });
});

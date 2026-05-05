import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useResearchSession } from "../hooks/useResearchSession";
import { makeSessionFull } from "../test/fixtures/research";
import { mockFetch } from "../test/server";

describe("useResearchSession", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("loads full session, then polls until finalized and stops", async () => {
    let call = 0;
    mockFetch({
      "/trading/api/research-pipeline/sessions/1?include=full": () => {
        call += 1;
        const status = call < 3 ? "running" : "finalized";
        return new Response(
          JSON.stringify(
            makeSessionFull({
              session: {
                ...makeSessionFull().session,
                status,
              },
            }),
          ),
        );
      },
    });

    const { result } = renderHook(() => useResearchSession(1));

    await waitFor(() => expect(result.current.status).toBe("success"));
    expect(result.current.data?.session.status).toBe("running");

    await vi.advanceTimersByTimeAsync(5000);
    await waitFor(() =>
      expect(result.current.data?.session.status).toBe("running"),
    );

    await vi.advanceTimersByTimeAsync(5000);
    await waitFor(() =>
      expect(result.current.data?.session.status).toBe("finalized"),
    );

    const callsAfterFinalized = call;
    await vi.advanceTimersByTimeAsync(15000);
    expect(call).toBe(callsAfterFinalized);
  });

  it("returns not_found on 404", async () => {
    mockFetch({
      "/trading/api/research-pipeline/sessions/999?include=full": () =>
        new Response(JSON.stringify({ detail: "session_not_found" }), {
          status: 404,
        }),
    });
    const { result } = renderHook(() => useResearchSession(999));
    await waitFor(() => expect(result.current.status).toBe("not_found"));
  });
});

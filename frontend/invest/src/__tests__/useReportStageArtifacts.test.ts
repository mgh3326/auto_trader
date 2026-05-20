// ROB-279 Phase 5 — useReportStageArtifacts hook tests.

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useReportStageArtifacts } from "../hooks/useReportStageArtifacts";

const originalFetch = global.fetch;

function makeArtifact(overrides: Record<string, unknown> = {}) {
  return {
    artifact_uuid: "art-1",
    run_uuid: "run-1",
    stage_type: "market",
    verdict: "bull",
    confidence: 80,
    summary: "Strong uptrend",
    key_points: ["momentum"],
    buy_evidence: [],
    sell_evidence: [],
    risk_evidence: [],
    missing_data: [],
    cited_snapshot_uuids: ["snap-1"],
    freshness_summary: null,
    model_name: "gemini-2.0-flash",
    prompt_version: "v1",
    payload_hash: null,
    raw_payload_json: null,
    created_at: "2026-05-20T12:00:00Z",
    ...overrides,
  };
}

function mockFetch(payload: unknown, status = 200) {
  global.fetch = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  }) as unknown as typeof fetch;
}

beforeEach(() => {
  global.fetch = vi.fn();
});

afterEach(() => {
  global.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe("useReportStageArtifacts", () => {
  it("starts with status 'loading' and no artifacts", () => {
    mockFetch({ report_uuid: "uuid-1", stage_run_uuid: null, artifacts: [] });

    const { result } = renderHook(() =>
      useReportStageArtifacts("uuid-1"),
    );

    expect(result.current.status).toBe("loading");
    expect(result.current.artifacts).toEqual([]);
    expect(result.current.error).toBeNull();
  });

  it("transitions to 'ready' and populates artifacts on success", async () => {
    mockFetch({
      report_uuid: "uuid-1",
      stage_run_uuid: "run-1",
      artifacts: [makeArtifact()],
    });

    const { result } = renderHook(() =>
      useReportStageArtifacts("uuid-1"),
    );

    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.artifacts).toHaveLength(1);
    expect(result.current.artifacts[0]!.artifactUuid).toBe("art-1");
    expect(result.current.stageRunUuid).toBe("run-1");
    expect(result.current.error).toBeNull();
  });

  it("transitions to 'error' when fetch throws", async () => {
    global.fetch = vi.fn().mockRejectedValue(
      new Error("network error"),
    ) as unknown as typeof fetch;

    const { result } = renderHook(() =>
      useReportStageArtifacts("uuid-1"),
    );

    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(result.current.error).toMatch(/network error/);
    expect(result.current.artifacts).toEqual([]);
  });

  it("transitions to 'error' when server returns non-OK", async () => {
    mockFetch({}, 404);

    const { result } = renderHook(() =>
      useReportStageArtifacts("uuid-1"),
    );

    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(result.current.error).toMatch(/404/);
  });

  it("stays 'loading' and does not fetch when reportUuid is undefined", async () => {
    const { result } = renderHook(() =>
      useReportStageArtifacts(undefined),
    );

    // Wait a tick to ensure no async transition happens
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });

    expect(result.current.status).toBe("loading");
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("reload increments call count (re-fetches)", async () => {
    mockFetch({
      report_uuid: "uuid-1",
      stage_run_uuid: "run-1",
      artifacts: [makeArtifact()],
    });

    const { result } = renderHook(() =>
      useReportStageArtifacts("uuid-1"),
    );

    await waitFor(() => expect(result.current.status).toBe("ready"));

    const fetchMock = global.fetch as ReturnType<typeof vi.fn>;
    const callsBefore = fetchMock.mock.calls.length;

    act(() => {
      result.current.reload();
    });

    await waitFor(() => {
      expect(
        (global.fetch as ReturnType<typeof vi.fn>).mock.calls.length,
      ).toBeGreaterThan(callsBefore);
    });
  });
});

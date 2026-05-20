// ROB-279 Phase 5 — investmentStages API client normalization tests.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchReportStageArtifacts } from "../api/investmentStages";

const originalFetch = global.fetch;

function mockFetchOnce(payload: unknown, status: number = 200): void {
  global.fetch = vi.fn().mockResolvedValueOnce({
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
});

const SNAKE_ARTIFACT = {
  artifact_uuid: "art-1",
  run_uuid: "run-1",
  stage_type: "market",
  verdict: "bull",
  confidence: 72,
  summary: "Market looks positive",
  key_points: ["Point A", "Point B"],
  buy_evidence: ["ev1"],
  sell_evidence: [],
  risk_evidence: ["risk1"],
  missing_data: [],
  cited_snapshot_uuids: ["snap-1", "snap-2"],
  freshness_summary: { overall: "fresh" },
  model_name: "gemini-2.0-flash",
  prompt_version: "v1",
  payload_hash: "abc123",
  raw_payload_json: { raw: true },
  created_at: "2026-05-20T12:00:00Z",
};

describe("fetchReportStageArtifacts", () => {
  it("normalises snake_case response to camelCase", async () => {
    mockFetchOnce({
      report_uuid: "uuid-1",
      stage_run_uuid: "run-1",
      artifacts: [SNAKE_ARTIFACT],
    });

    const result = await fetchReportStageArtifacts("uuid-1");

    expect(result.reportUuid).toBe("uuid-1");
    expect(result.stageRunUuid).toBe("run-1");
    expect(result.artifacts).toHaveLength(1);

    const art = result.artifacts[0]!;
    expect(art.artifactUuid).toBe("art-1");
    expect(art.runUuid).toBe("run-1");
    expect(art.stageType).toBe("market");
    expect(art.verdict).toBe("bull");
    expect(art.confidence).toBe(72);
    expect(art.summary).toBe("Market looks positive");
    expect(art.keyPoints).toEqual(["Point A", "Point B"]);
    expect(art.buyEvidence).toEqual(["ev1"]);
    expect(art.sellEvidence).toEqual([]);
    expect(art.riskEvidence).toEqual(["risk1"]);
    expect(art.missingData).toEqual([]);
    expect(art.citedSnapshotUuids).toEqual(["snap-1", "snap-2"]);
    expect(art.freshnessSummary).toEqual({ overall: "fresh" });
    expect(art.modelName).toBe("gemini-2.0-flash");
    expect(art.promptVersion).toBe("v1");
    expect(art.payloadHash).toBe("abc123");
    expect(art.rawPayloadJson).toEqual({ raw: true });
    expect(art.createdAt).toBe("2026-05-20T12:00:00Z");
  });

  it("handles null stageRunUuid gracefully", async () => {
    mockFetchOnce({
      report_uuid: "uuid-2",
      stage_run_uuid: null,
      artifacts: [],
    });

    const result = await fetchReportStageArtifacts("uuid-2");
    expect(result.stageRunUuid).toBeNull();
    expect(result.artifacts).toHaveLength(0);
  });

  it("handles null/missing optional fields on an artifact", async () => {
    mockFetchOnce({
      report_uuid: "uuid-3",
      stage_run_uuid: null,
      artifacts: [
        {
          artifact_uuid: "art-2",
          run_uuid: "run-2",
          stage_type: "news",
          verdict: "neutral",
          confidence: 0,
          summary: null,
          key_points: [],
          buy_evidence: [],
          sell_evidence: [],
          risk_evidence: [],
          missing_data: ["news_api"],
          cited_snapshot_uuids: [],
          freshness_summary: null,
          model_name: null,
          prompt_version: null,
          payload_hash: null,
          raw_payload_json: null,
          created_at: "2026-05-20T12:00:00Z",
        },
      ],
    });

    const result = await fetchReportStageArtifacts("uuid-3");
    const art = result.artifacts[0]!;
    expect(art.summary).toBeNull();
    expect(art.freshnessSummary).toBeNull();
    expect(art.modelName).toBeNull();
    expect(art.missingData).toEqual(["news_api"]);
  });

  it("URL-encodes the report_uuid", async () => {
    mockFetchOnce({
      report_uuid: "uuid with space",
      stage_run_uuid: null,
      artifacts: [],
    });

    await fetchReportStageArtifacts("uuid with space");
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining("uuid%20with%20space"),
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("throws an error including the status code on non-OK response", async () => {
    mockFetchOnce({}, 404);
    await expect(fetchReportStageArtifacts("uuid-1")).rejects.toThrow(/404/);
  });

  it("throws an error on 500 response", async () => {
    mockFetchOnce({ detail: "internal error" }, 500);
    await expect(fetchReportStageArtifacts("uuid-1")).rejects.toThrow(/500/);
  });
});

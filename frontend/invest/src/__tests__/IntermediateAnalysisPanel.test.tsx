// ROB-279 Phase 5 — IntermediateAnalysisPanel tests.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

import { IntermediateAnalysisPanel } from "../components/investment-reports/IntermediateAnalysisPanel";

const originalFetch = global.fetch;

interface FetchResponseInit {
  status?: number;
  ok?: boolean;
  json: () => Promise<unknown>;
}

function makeResponse(payload: unknown, status: number = 200): FetchResponseInit {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => payload,
  };
}

function makeArtifact(
  stageType: string,
  overrides: Record<string, unknown> = {},
) {
  return {
    artifact_uuid: `art-${stageType}`,
    run_uuid: "run-1",
    stage_type: stageType,
    verdict: "bull",
    confidence: 70,
    summary: `Summary for ${stageType}`,
    key_points: [],
    buy_evidence: [],
    sell_evidence: [],
    risk_evidence: [],
    missing_data: [],
    cited_snapshot_uuids: [],
    freshness_summary: null,
    model_name: "gemini-2.0-flash",
    prompt_version: "v1",
    payload_hash: null,
    raw_payload_json: null,
    created_at: "2026-05-20T12:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  global.fetch = vi.fn();
});

afterEach(() => {
  global.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe("IntermediateAnalysisPanel", () => {
  it("shows loading state initially", () => {
    // Fetch that never resolves (simulate slow network)
    global.fetch = vi.fn().mockReturnValue(new Promise(() => {})) as unknown as typeof fetch;

    render(<IntermediateAnalysisPanel reportUuid="uuid-1" />);
    expect(
      screen.getByTestId("intermediate-analysis-panel-loading"),
    ).toBeInTheDocument();
  });

  it("renders all stage cards after data loads", async () => {
    const stageTypes = ["market", "news", "portfolio_journal"];
    (global.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      makeResponse({
        report_uuid: "uuid-1",
        stage_run_uuid: "run-1",
        artifacts: stageTypes.map((t) => makeArtifact(t)),
      }),
    );

    render(<IntermediateAnalysisPanel reportUuid="uuid-1" />);

    await waitFor(() =>
      expect(
        screen.getByTestId("intermediate-analysis-panel"),
      ).toBeInTheDocument(),
    );

    for (const stageType of stageTypes) {
      expect(
        screen.getByTestId(`stage-card-${stageType}`),
      ).toBeInTheDocument();
    }

    expect(screen.getByText("중간 분석")).toBeInTheDocument();
  });

  it("shows empty state when artifacts is empty", async () => {
    (global.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      makeResponse({
        report_uuid: "uuid-1",
        stage_run_uuid: null,
        artifacts: [],
      }),
    );

    render(<IntermediateAnalysisPanel reportUuid="uuid-1" />);

    await waitFor(() =>
      expect(
        screen.getByTestId("intermediate-analysis-panel-empty"),
      ).toBeInTheDocument(),
    );

    expect(
      screen.getByText(
        "중간 분석 결과가 없습니다 (legacy 또는 auto_compose=false 리포트).",
      ),
    ).toBeInTheDocument();
  });

  it("shows error state on fetch failure with reload button", async () => {
    (global.fetch as ReturnType<typeof vi.fn>)
      .mockRejectedValueOnce(new Error("network failure"))
      .mockResolvedValue(
        makeResponse({
          report_uuid: "uuid-1",
          stage_run_uuid: "run-1",
          artifacts: [makeArtifact("market")],
        }),
      );

    render(<IntermediateAnalysisPanel reportUuid="uuid-1" />);

    await waitFor(() =>
      expect(
        screen.getByTestId("intermediate-analysis-panel-error"),
      ).toBeInTheDocument(),
    );

    expect(screen.getByText(/network failure/)).toBeInTheDocument();

    const reloadButton = screen.getByRole("button", { name: "다시 시도" });
    expect(reloadButton).toBeInTheDocument();

    fireEvent.click(reloadButton);

    await waitFor(() =>
      expect(
        screen.getByTestId("intermediate-analysis-panel"),
      ).toBeInTheDocument(),
    );
  });

  it("shows error state on non-OK HTTP response", async () => {
    (global.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      makeResponse({}, 404),
    );

    render(<IntermediateAnalysisPanel reportUuid="uuid-missing" />);

    await waitFor(() =>
      expect(
        screen.getByTestId("intermediate-analysis-panel-error"),
      ).toBeInTheDocument(),
    );

    expect(screen.getByText(/404/)).toBeInTheDocument();
  });
});

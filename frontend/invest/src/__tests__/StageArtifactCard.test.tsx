// ROB-279 Phase 5 — StageArtifactCard unit tests.

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { StageArtifactCard } from "../components/investment-reports/StageArtifactCard";
import type { StageArtifact } from "../types/investmentReports";

function makeArtifact(overrides: Partial<StageArtifact> = {}): StageArtifact {
  return {
    artifactUuid: "art-1",
    runUuid: "run-1",
    stageType: "market",
    verdict: "bull",
    confidence: 85,
    summary: "Strong market conditions",
    keyPoints: ["Momentum positive", "Volume high"],
    buyEvidence: [],
    sellEvidence: [],
    riskEvidence: [],
    missingData: [],
    citedSnapshotUuids: ["snap-1", "snap-2", "snap-3"],
    freshnessSummary: null,
    modelName: "gemini-2.0-flash",
    promptVersion: "v1",
    payloadHash: null,
    rawPayloadJson: null,
    createdAt: "2026-05-20T12:00:00Z",
    ...overrides,
  };
}

describe("StageArtifactCard", () => {
  it("renders the stage type label in Korean", () => {
    render(<StageArtifactCard artifact={makeArtifact({ stageType: "market" })} />);
    expect(screen.getByText("시장")).toBeInTheDocument();
  });

  it("renders the verdict label in Korean", () => {
    render(<StageArtifactCard artifact={makeArtifact({ verdict: "bull" })} />);
    expect(screen.getByText("매수 측")).toBeInTheDocument();
  });

  it("renders bear verdict label", () => {
    render(<StageArtifactCard artifact={makeArtifact({ verdict: "bear" })} />);
    expect(screen.getByText("매도 측")).toBeInTheDocument();
  });

  it("renders neutral verdict label", () => {
    render(<StageArtifactCard artifact={makeArtifact({ verdict: "neutral" })} />);
    expect(screen.getByText("중립")).toBeInTheDocument();
  });

  it("renders unavailable verdict label", () => {
    render(
      <StageArtifactCard artifact={makeArtifact({ verdict: "unavailable" })} />,
    );
    expect(screen.getByText("확인 불가")).toBeInTheDocument();
  });

  it("renders confidence value", () => {
    render(<StageArtifactCard artifact={makeArtifact({ confidence: 85 })} />);
    expect(screen.getByText("85")).toBeInTheDocument();
  });

  it("renders zero confidence", () => {
    render(<StageArtifactCard artifact={makeArtifact({ confidence: 0 })} />);
    expect(screen.getByText("0")).toBeInTheDocument();
  });

  it("renders the summary text", () => {
    render(
      <StageArtifactCard
        artifact={makeArtifact({ summary: "Strong market conditions" })}
      />,
    );
    expect(screen.getByText("Strong market conditions")).toBeInTheDocument();
  });

  it("does not render summary section when summary is null", () => {
    render(<StageArtifactCard artifact={makeArtifact({ summary: null })} />);
    expect(screen.queryByText("Strong market conditions")).not.toBeInTheDocument();
  });

  it("renders key points as a list", () => {
    render(
      <StageArtifactCard
        artifact={makeArtifact({ keyPoints: ["Point A", "Point B"] })}
      />,
    );
    expect(screen.getByText("Point A")).toBeInTheDocument();
    expect(screen.getByText("Point B")).toBeInTheDocument();
  });

  it("renders missing_data chips with '누락 데이터:' prefix", () => {
    render(
      <StageArtifactCard
        artifact={makeArtifact({ missingData: ["news_api", "portfolio"] })}
      />,
    );
    expect(screen.getByText("누락 데이터: news_api")).toBeInTheDocument();
    expect(screen.getByText("누락 데이터: portfolio")).toBeInTheDocument();
  });

  it("renders the citation count in the footer", () => {
    render(
      <StageArtifactCard
        artifact={makeArtifact({
          citedSnapshotUuids: ["snap-1", "snap-2", "snap-3"],
        })}
      />,
    );
    expect(screen.getByText("근거 스냅샷 3개")).toBeInTheDocument();
  });

  it("renders model name in the footer", () => {
    render(
      <StageArtifactCard
        artifact={makeArtifact({ modelName: "gemini-2.0-flash" })}
      />,
    );
    expect(screen.getByText("gemini-2.0-flash")).toBeInTheDocument();
  });

  it("omits model name from footer when null", () => {
    render(<StageArtifactCard artifact={makeArtifact({ modelName: null })} />);
    // Model name should not appear; the separator '·' should not appear either
    expect(screen.queryByText("gemini-2.0-flash")).not.toBeInTheDocument();
  });

  it("applies the correct data-testid to the article", () => {
    render(
      <StageArtifactCard artifact={makeArtifact({ stageType: "risk_review" })} />,
    );
    expect(screen.getByTestId("stage-card-risk_review")).toBeInTheDocument();
  });

  it("renders all 8 stage type labels", () => {
    const stages: Array<StageArtifact["stageType"]> = [
      "market",
      "news",
      "portfolio_journal",
      "watch_context",
      "candidate_universe",
      "bull_reducer",
      "bear_reducer",
      "risk_review",
    ];
    const expectedLabels = [
      "시장",
      "뉴스",
      "포트폴리오·저널",
      "와치 컨텍스트",
      "후보 종목",
      "매수 측 요약",
      "매도/위험 측 요약",
      "리스크 리뷰",
    ];

    for (let i = 0; i < stages.length; i++) {
      const { unmount } = render(
        <StageArtifactCard artifact={makeArtifact({ stageType: stages[i] })} />,
      );
      expect(screen.getByText(expectedLabels[i]!)).toBeInTheDocument();
      unmount();
    }
  });
});

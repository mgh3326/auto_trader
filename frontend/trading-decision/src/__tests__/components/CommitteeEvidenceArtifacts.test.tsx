import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { CommitteeEvidenceArtifacts } from "../../components/CommitteeEvidenceArtifacts";
import type { CommitteeArtifacts } from "../../api/types";

describe("CommitteeEvidenceArtifacts", () => {
  it("renders evidence when present", () => {
    const artifacts: CommitteeArtifacts = {
      evidence: {
        technical_analysis: { summary: "Bullish trend", confidence: 80, payload: null },
        news_analysis: { summary: "Positive earnings", confidence: 90, payload: null },
        on_chain_analysis: null,
      },
      research_debate: null,
      trader_draft: null,
      risk_review: null,
      portfolio_approval: null,
      execution_preview: null,
      journal_placeholder: null,
    };

    render(<CommitteeEvidenceArtifacts artifacts={artifacts} />);

    expect(screen.getByText("위원회 근거 자료")).toBeInTheDocument();
    expect(screen.getByText("Bullish trend")).toBeInTheDocument();
    expect(screen.getByText("신뢰도: 80%")).toBeInTheDocument();
    expect(screen.getByText("Positive earnings")).toBeInTheDocument();
    expect(screen.getByText("신뢰도: 90%")).toBeInTheDocument();
  });

  it("renders nothing if evidence is missing", () => {
    const { container } = render(<CommitteeEvidenceArtifacts artifacts={null} />);
    expect(container.firstChild).toBeNull();
  });
});

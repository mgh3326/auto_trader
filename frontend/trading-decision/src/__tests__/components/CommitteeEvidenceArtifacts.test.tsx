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
      risk_review: null,
      portfolio_approval: null,
      execution_preview: null,
      journal_placeholder: null,
    };

    render(<CommitteeEvidenceArtifacts artifacts={artifacts} />);

    expect(screen.getByText("Committee Evidence")).toBeInTheDocument();
    expect(screen.getByText("Bullish trend")).toBeInTheDocument();
    expect(screen.getByText("Confidence: 80%")).toBeInTheDocument();
    expect(screen.getByText("Positive earnings")).toBeInTheDocument();
    expect(screen.getByText("Confidence: 90%")).toBeInTheDocument();
  });

  it("renders nothing if evidence is missing", () => {
    const { container } = render(<CommitteeEvidenceArtifacts artifacts={null} />);
    expect(container.firstChild).toBeNull();
  });
});

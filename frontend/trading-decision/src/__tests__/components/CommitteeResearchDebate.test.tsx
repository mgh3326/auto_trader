import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { CommitteeResearchDebate } from "../../components/CommitteeResearchDebate";
import type { CommitteeResearchDebate as DebateType } from "../../api/types";

describe("CommitteeResearchDebate", () => {
  it("renders bull and bear claims with weight and source", () => {
    const debate: DebateType = {
      bull_case: [
        { text: "Support bounce", weight: "high", source: "technical" },
      ],
      bear_case: [
        { text: "RSI overbought", weight: "medium", source: "technical" },
      ],
      summary: "1 bull / 1 bear",
    };

    render(<CommitteeResearchDebate researchDebate={debate} />);

    expect(screen.getByText("리서치 토론")).toBeInTheDocument();
    expect(screen.getByText("상승 근거")).toBeInTheDocument();
    expect(screen.getByText("하락 근거")).toBeInTheDocument();
    expect(screen.getByText("Support bounce")).toBeInTheDocument();
    expect(screen.getByText("RSI overbought")).toBeInTheDocument();
    expect(screen.getByText("1 bull / 1 bear")).toBeInTheDocument();
  });

  it("shows empty placeholders when both cases are empty", () => {
    const debate: DebateType = {
      bull_case: [],
      bear_case: [],
      summary: "no signal",
    };

    render(<CommitteeResearchDebate researchDebate={debate} />);

    expect(screen.getByText("아직 상승 근거가 없습니다.")).toBeInTheDocument();
    expect(screen.getByText("아직 하락 근거가 없습니다.")).toBeInTheDocument();
  });

  it("renders nothing when debate is null", () => {
    const { container } = render(
      <CommitteeResearchDebate researchDebate={null} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when debate has no content at all", () => {
    const debate: DebateType = {
      bull_case: [],
      bear_case: [],
      summary: null,
    };
    const { container } = render(
      <CommitteeResearchDebate researchDebate={debate} />,
    );
    expect(container.firstChild).toBeNull();
  });
});

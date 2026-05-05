import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { CommitteeRiskReview } from "../../components/CommitteeRiskReview";
import type { CommitteeRiskReview as RiskReviewType } from "../../api/types";

describe("CommitteeRiskReview", () => {
  it("renders risk review when present", () => {
    const riskReview: RiskReviewType = {
      verdict: "approved",
      notes: "All checks passed",
      reviewed_at: "2026-05-05T08:00:00Z",
    };

    render(<CommitteeRiskReview riskReview={riskReview} />);

    expect(screen.getByText("리스크 리뷰")).toBeInTheDocument();
    expect(screen.getByText("승인됨")).toBeInTheDocument();
    expect(screen.getByText("All checks passed")).toBeInTheDocument();
  });

  it("renders nothing if risk review is missing", () => {
    const { container } = render(<CommitteeRiskReview riskReview={null} />);
    expect(container.firstChild).toBeNull();
  });
});

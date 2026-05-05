import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { CommitteePortfolioApproval } from "../../components/CommitteePortfolioApproval";
import type { CommitteePortfolioApproval as PortfolioApprovalType } from "../../api/types";

describe("CommitteePortfolioApproval", () => {
  it("renders portfolio approval when present", () => {
    const portfolioApproval: PortfolioApprovalType = {
      verdict: "approved",
      notes: "Portfolio weight adjusted",
      approved_at: "2026-05-05T08:00:00Z",
    };

    render(<CommitteePortfolioApproval portfolioApproval={portfolioApproval} />);

    expect(screen.getByText("포트폴리오 승인")).toBeInTheDocument();
    expect(screen.getByText("승인됨")).toBeInTheDocument();
    expect(screen.getByText("Portfolio weight adjusted")).toBeInTheDocument();
  });

  it("renders nothing if portfolio approval is missing", () => {
    const { container } = render(<CommitteePortfolioApproval portfolioApproval={null} />);
    expect(container.firstChild).toBeNull();
  });
});

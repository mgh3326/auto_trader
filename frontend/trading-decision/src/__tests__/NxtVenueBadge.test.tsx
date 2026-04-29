import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import NxtVenueBadge from "../components/NxtVenueBadge";

describe("NxtVenueBadge", () => {
  it("renders 'NXT actionable' for KR + actionable + nxt_eligible=true", () => {
    render(
      <NxtVenueBadge
        marketScope="kr"
        nxtClassification="buy_pending_actionable"
        nxtEligible={true}
      />,
    );
    expect(screen.getByText("NXT actionable")).toBeInTheDocument();
  });

  it("renders 'NXT not actionable' for too-far / ignore_for_nxt", () => {
    render(
      <NxtVenueBadge
        marketScope="kr"
        nxtClassification="buy_pending_too_far"
        nxtEligible={true}
      />,
    );
    expect(screen.getByText("NXT not actionable")).toBeInTheDocument();
  });

  it("renders 'Non-NXT (KR broker)' when nxt_eligible=false", () => {
    render(
      <NxtVenueBadge
        marketScope="kr"
        nxtClassification="non_nxt_pending_ignore_for_nxt"
        nxtEligible={false}
      />,
    );
    const badge = screen.getByText("Non-NXT (KR broker)");
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveAccessibleName("NXT venue: Non-NXT (KR broker)");
  });

  it("renders 'NXT eligibility unknown' when nxt_eligible is null", () => {
    render(
      <NxtVenueBadge
        marketScope="kr"
        nxtClassification={null}
        nxtEligible={null}
      />,
    );
    expect(screen.getByText("NXT eligibility unknown")).toBeInTheDocument();
  });

  it("renders 'NXT review needed' for data_mismatch_requires_review", () => {
    render(
      <NxtVenueBadge
        marketScope="kr"
        nxtClassification="data_mismatch_requires_review"
        nxtEligible={true}
      />,
    );
    expect(screen.getByText("NXT review needed")).toBeInTheDocument();
  });

  it("renders nothing for non-KR markets", () => {
    const { container } = render(
      <NxtVenueBadge
        marketScope="us"
        nxtClassification={null}
        nxtEligible={null}
      />,
    );
    expect(container.firstChild).toBeNull();
  });
});

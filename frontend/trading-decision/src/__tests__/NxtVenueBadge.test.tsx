import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import NxtVenueBadge from "../components/NxtVenueBadge";

describe("NxtVenueBadge", () => {
  it("renders 'NXT 실행 가능' for KR + actionable + nxt_eligible=true", () => {
    render(
      <NxtVenueBadge
        marketScope="kr"
        nxtClassification="buy_pending_actionable"
        nxtEligible={true}
      />,
    );
    expect(screen.getByText("NXT 실행 가능")).toBeInTheDocument();
  });

  it("renders 'NXT 실행 불가' for too-far / ignore_for_nxt", () => {
    render(
      <NxtVenueBadge
        marketScope="kr"
        nxtClassification="buy_pending_too_far"
        nxtEligible={true}
      />,
    );
    expect(screen.getByText("NXT 실행 불가")).toBeInTheDocument();
  });

  it("renders '비-NXT (국내 브로커)' when nxt_eligible=false", () => {
    render(
      <NxtVenueBadge
        marketScope="kr"
        nxtClassification="non_nxt_pending_ignore_for_nxt"
        nxtEligible={false}
      />,
    );
    const badge = screen.getByText("비-NXT (국내 브로커)");
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveAccessibleName("NXT 거래소: 비-NXT (국내 브로커)");
  });

  it("renders 'NXT 자격 알 수 없음' when nxt_eligible is null", () => {
    render(
      <NxtVenueBadge
        marketScope="kr"
        nxtClassification={null}
        nxtEligible={null}
      />,
    );
    expect(screen.getByText("NXT 자격 알 수 없음")).toBeInTheDocument();
  });

  it("renders 'NXT 검토 필요' for data_mismatch_requires_review", () => {
    render(
      <NxtVenueBadge
        marketScope="kr"
        nxtClassification="data_mismatch_requires_review"
        nxtEligible={true}
      />,
    );
    expect(screen.getByText("NXT 검토 필요")).toBeInTheDocument();
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

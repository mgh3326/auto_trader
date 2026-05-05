import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ReconciliationBadge from "../components/ReconciliationBadge";

describe("ReconciliationBadge", () => {
  it("renders a label for each known classification", () => {
    const cases: Array<[string, string]> = [
      ["maintain", "유지"],
      ["near_fill", "체결 임박"],
      ["too_far", "괴리 큼"],
      ["chasing_risk", "추격 위험"],
      ["data_mismatch", "데이터 불일치"],
      ["kr_pending_non_nxt", "국내 브로커 전용"],
      ["unknown_venue", "거래소 알 수 없음"],
      ["unknown", "알 수 없음"],
    ];
    for (const [value, label] of cases) {
      const { unmount } = render(
        <ReconciliationBadge
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          value={value as any}
        />,
      );
      expect(screen.getByText(label)).toBeInTheDocument();
      unmount();
    }
  });

  it("renders nothing when value is null", () => {
    const { container } = render(<ReconciliationBadge value={null} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders an aria-label for accessibility", () => {
    render(<ReconciliationBadge value="too_far" />);
    expect(
      screen.getByLabelText("조정 상태: 괴리 큼"),
    ).toBeInTheDocument();
  });
});

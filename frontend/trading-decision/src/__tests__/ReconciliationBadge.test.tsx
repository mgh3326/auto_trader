import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ReconciliationBadge from "../components/ReconciliationBadge";

describe("ReconciliationBadge", () => {
  it("renders a label for each known classification", () => {
    const cases: Array<[string, string]> = [
      ["maintain", "Maintain"],
      ["near_fill", "Near fill"],
      ["too_far", "Too far"],
      ["chasing_risk", "Chasing risk"],
      ["data_mismatch", "Data mismatch"],
      ["kr_pending_non_nxt", "KR broker only"],
      ["unknown_venue", "Unknown venue"],
      ["unknown", "Unknown"],
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
      screen.getByLabelText("Reconciliation status: Too far"),
    ).toBeInTheDocument();
  });
});

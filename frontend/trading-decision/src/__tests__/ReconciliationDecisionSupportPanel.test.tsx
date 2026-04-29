import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ReconciliationDecisionSupportPanel from "../components/ReconciliationDecisionSupportPanel";
import { makeReconciliationPayload } from "../test/fixtures";

describe("ReconciliationDecisionSupportPanel", () => {
  it("renders gap, distance to fill, support/resistance, spread, and live quote", () => {
    render(
      <ReconciliationDecisionSupportPanel
        side="buy"
        originalPrice="70000"
        originalQuantity="10"
        payload={makeReconciliationPayload()}
      />,
    );

    expect(screen.getByText(/Gap to current/)).toBeInTheDocument();
    expect(screen.getByText("+0.29%")).toBeInTheDocument();
    expect(screen.getByText(/Distance to fill/)).toBeInTheDocument();
    expect(screen.getByText("-0.29%")).toBeInTheDocument();
    expect(screen.getByText(/Nearest support/)).toBeInTheDocument();
    expect(screen.getByText(/69,500/)).toBeInTheDocument();
    expect(screen.getByText(/Nearest resistance/)).toBeInTheDocument();
    expect(screen.getByText(/Live quote/)).toBeInTheDocument();
    expect(screen.getByText(/70,200/)).toBeInTheDocument();
    expect(screen.getByText(/Pending order/)).toBeInTheDocument();
    expect(screen.getByText(/ORD-1/)).toBeInTheDocument();
  });

  it("renders em-dash for missing decimal fields", () => {
    render(
      <ReconciliationDecisionSupportPanel
        side="buy"
        originalPrice={null}
        originalQuantity={null}
        payload={makeReconciliationPayload({
          decision_support: {
            current_price: null,
            gap_pct: null,
            signed_distance_to_fill: null,
            nearest_support_price: null,
            nearest_support_distance_pct: null,
            nearest_resistance_price: null,
            nearest_resistance_distance_pct: null,
            bid_ask_spread_pct: null,
          },
          live_quote: null,
        })}
      />,
    );
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(4);
  });

  it("returns null when payload is null", () => {
    const { container } = render(
      <ReconciliationDecisionSupportPanel
        side="buy"
        originalPrice="70000"
        originalQuantity="10"
        payload={null}
      />,
    );
    expect(container.firstChild).toBeNull();
  });
});

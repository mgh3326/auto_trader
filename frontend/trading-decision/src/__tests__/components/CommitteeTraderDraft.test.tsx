import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { CommitteeTraderDraft } from "../../components/CommitteeTraderDraft";
import type { CommitteeTraderDraft as DraftType } from "../../api/types";

describe("CommitteeTraderDraft", () => {
  it("renders the draft action, symbol, and details", () => {
    const drafts: DraftType[] = [
      {
        symbol: "AAPL",
        action: "BUY",
        price_plan: "limit @ 180.00",
        size_plan: "1 share",
        rationale: "support bounce",
        confidence: "medium",
        invalidation_condition: "close below 175",
        next_step_recommendation: "watch open",
        is_live_order: false,
      },
    ];

    render(<CommitteeTraderDraft traderDraft={drafts} />);

    expect(screen.getByText("Trader Draft")).toBeInTheDocument();
    expect(screen.getByText("BUY")).toBeInTheDocument();
    expect(screen.getByText("AAPL")).toBeInTheDocument();
    expect(screen.getByText("limit @ 180.00")).toBeInTheDocument();
    expect(screen.getByText("close below 175")).toBeInTheDocument();
  });

  it("clearly labels the draft as not a live order", () => {
    const drafts: DraftType[] = [
      {
        symbol: "AAPL",
        action: "BUY",
        price_plan: null,
        size_plan: null,
        rationale: null,
        confidence: "low",
        invalidation_condition: null,
        next_step_recommendation: null,
        is_live_order: false,
      },
    ];

    render(<CommitteeTraderDraft traderDraft={drafts} />);

    expect(
      screen.getByText(/no live order is created/i),
    ).toBeInTheDocument();
  });

  it("renders nothing when draft is null or empty", () => {
    const a = render(<CommitteeTraderDraft traderDraft={null} />);
    expect(a.container.firstChild).toBeNull();

    const b = render(<CommitteeTraderDraft traderDraft={[]} />);
    expect(b.container.firstChild).toBeNull();
  });
});

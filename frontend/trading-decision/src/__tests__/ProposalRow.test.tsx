import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ProposalRow from "../components/ProposalRow";
import { makeAction, makeProposal } from "../test/fixtures";

describe("ProposalRow", () => {
  it("pending proposal shows original block only", () => {
    render(<ProposalRow proposal={makeProposal()} onRespond={vi.fn()} />);

    expect(screen.getByText("Original")).toBeInTheDocument();
    expect(screen.queryByText("Your decision")).not.toBeInTheDocument();
  });

  it("accepted proposal shows decision and responded time", () => {
    render(
      <ProposalRow
        proposal={makeProposal({
          user_response: "accept",
          responded_at: "2026-04-28T07:00:00Z",
        })}
        onRespond={vi.fn()}
      />,
    );

    expect(screen.getAllByText("accept").length).toBeGreaterThan(0);
    expect(screen.getByText("Your decision")).toBeInTheDocument();
  });

  it("shows original and adjusted values for modify", () => {
    render(
      <ProposalRow
        proposal={makeProposal({
          user_response: "modify",
          responded_at: "2026-04-28T07:00:00Z",
          user_quantity_pct: "10",
        })}
        onRespond={vi.fn()}
      />,
    );

    expect(screen.getAllByText("20").length).toBeGreaterThan(0);
    expect(screen.getByText("10")).toBeInTheDocument();
  });

  it("renders linked action rows", () => {
    render(
      <ProposalRow
        proposal={makeProposal({ actions: [makeAction({ external_order_id: "LIVE-1" })] })}
        onRespond={vi.fn()}
      />,
    );

    expect(screen.getByText("LIVE-1")).toBeInTheDocument();
  });
});

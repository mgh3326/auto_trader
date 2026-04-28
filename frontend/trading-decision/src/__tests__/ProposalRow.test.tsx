import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import ProposalRow from "../components/ProposalRow";
import {
  makeAction,
  makeCounterfactual,
  makeOutcome,
  makeProposal,
} from "../test/fixtures";

describe("ProposalRow", () => {
  it("shows the payload display name prominently with the symbol as secondary text", () => {
    render(
      <ProposalRow
        proposal={makeProposal({
          symbol: "035420",
          original_payload: { name: "NAVER" },
        })}
        onRecordOutcome={vi.fn()}
        onRespond={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { name: "NAVER" })).toBeInTheDocument();
    expect(screen.getByText("035420")).toBeInTheDocument();
  });

  it("does not show a zero KRW amount as actionable when a sell amount is missing", () => {
    render(
      <ProposalRow
        proposal={makeProposal({
          original_amount: "0",
          original_price: null,
          original_quantity: "3",
          side: "sell",
        })}
        onRecordOutcome={vi.fn()}
        onRespond={vi.fn()}
      />,
    );

    expect(screen.queryByText("0 KRW")).not.toBeInTheDocument();
    expect(screen.getByText("Current quote estimate needed")).toBeInTheDocument();
  });

  it("explains that accepting records a decision only", () => {
    render(
      <ProposalRow
        proposal={makeProposal()}
        onRecordOutcome={vi.fn()}
        onRespond={vi.fn()}
      />,
    );

    expect(
      screen.getByText(/Accept records this decision only/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/does not send a live trade/i)).toBeInTheDocument();
  });

  it("pending proposal shows original block only", () => {
    render(
      <ProposalRow
        proposal={makeProposal()}
        onRecordOutcome={vi.fn()}
        onRespond={vi.fn()}
      />,
    );

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
        onRecordOutcome={vi.fn()}
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
        onRecordOutcome={vi.fn()}
        onRespond={vi.fn()}
      />,
    );

    expect(screen.getAllByText("20").length).toBeGreaterThan(0);
    expect(screen.getByText("10")).toBeInTheDocument();
  });

  it("renders linked action rows", () => {
    render(
      <ProposalRow
        proposal={makeProposal({
          actions: [makeAction({ external_order_id: "LIVE-1" })],
        })}
        onRecordOutcome={vi.fn()}
        onRespond={vi.fn()}
      />,
    );

    expect(screen.getByText("LIVE-1")).toBeInTheDocument();
  });

  it("renders outcome marks for the proposal", () => {
    render(
      <ProposalRow
        proposal={makeProposal({
          outcomes: [makeOutcome({ pnl_pct: "2.5000" })],
        })}
        onRecordOutcome={vi.fn()}
        onRespond={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("table", { name: /outcome marks/i }),
    ).toBeInTheDocument();
    expect(screen.getByText("2.5%")).toBeInTheDocument();
  });

  it("submits outcome marks through onRecordOutcome", async () => {
    const onRecordOutcome = vi.fn().mockResolvedValue({ ok: true });
    render(
      <ProposalRow
        proposal={makeProposal({
          counterfactuals: [
            makeCounterfactual({
              id: 11,
              track_kind: "rejected_counterfactual",
            }),
          ],
        })}
        onRecordOutcome={onRecordOutcome}
        onRespond={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByText(/record outcome mark/i));
    await userEvent.type(screen.getByLabelText(/price at mark/i), "100");
    await userEvent.click(screen.getByRole("button", { name: /record mark/i }));

    expect(onRecordOutcome).toHaveBeenCalledWith(
      "proposal-btc",
      expect.objectContaining({
        track_kind: "accepted_live",
        horizon: "1h",
        price_at_mark: "100",
      }),
    );
  });
});

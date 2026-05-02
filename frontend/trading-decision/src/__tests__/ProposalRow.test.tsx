import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import ProposalRow from "../components/ProposalRow";
import {
  makeAction,
  makeCounterfactual,
  makeOutcome,
  makeProposal,
  makeReconciliationPayload,
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

  it("renders crypto paper workflow provenance from approval copy", () => {
    render(
      <ProposalRow
        proposal={makeProposal({
          symbol: "KRW-BTC",
          instrument_type: "crypto",
          side: "buy",
          proposal_kind: "pullback_watch",
          original_payload: {
            crypto_paper_workflow: {
              signal_symbol: "KRW-BTC",
              signal_venue: "upbit",
              execution_symbol: "BTC/USD",
              execution_venue: "alpaca_paper",
              asset_class: "crypto",
              execution_mode: "paper",
              stage: "crypto_weekend",
              purpose: "paper_plumbing_smoke",
              preview_payload: {
                symbol: "BTC/USD",
                side: "buy",
                type: "limit",
                notional: "10",
                limit_price: "1.00",
                time_in_force: "gtc",
                asset_class: "crypto",
              },
              approval_copy: [
                "Signal source: Upbit KRW-BTC",
                "Execution venue: Alpaca Paper BTC/USD",
                "Purpose: paper_plumbing_smoke",
                "Order: buy limit $10 @ $1.00 GTC",
              ],
            },
          },
        })}
        onRecordOutcome={vi.fn()}
        onRespond={vi.fn()}
      />,
    );

    expect(screen.getByText("Crypto paper workflow")).toBeInTheDocument();
    expect(screen.getByText("Signal source: Upbit KRW-BTC")).toBeInTheDocument();
    expect(
      screen.getByText("Execution venue: Alpaca Paper BTC/USD"),
    ).toBeInTheDocument();
    expect(screen.getByText("Purpose: paper_plumbing_smoke")).toBeInTheDocument();
    expect(screen.getByText("Order: buy limit $10 @ $1.00 GTC")).toBeInTheDocument();
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

describe("ProposalRow — reconciliation/NXT badges", () => {
  it("renders the Near fill badge for near_fill", () => {
    render(
      <ProposalRow
        proposal={makeProposal({
          instrument_type: "equity_kr",
          original_payload: makeReconciliationPayload({
            reconciliation_status: "near_fill",
            nxt_classification: "buy_pending_actionable",
          }) as unknown as Record<string, unknown>,
        })}
        onRecordOutcome={vi.fn()}
        onRespond={vi.fn()}
      />,
    );
    expect(screen.getByText("Near fill")).toBeInTheDocument();
    expect(screen.getByText("NXT actionable")).toBeInTheDocument();
  });

  it("renders the Too far badge for too_far", () => {
    render(
      <ProposalRow
        proposal={makeProposal({
          instrument_type: "equity_kr",
          original_payload: makeReconciliationPayload({
            reconciliation_status: "too_far",
            nxt_classification: "buy_pending_too_far",
          }) as unknown as Record<string, unknown>,
        })}
        onRecordOutcome={vi.fn()}
        onRespond={vi.fn()}
      />,
    );
    expect(screen.getByText("Too far")).toBeInTheDocument();
    expect(screen.getByText("NXT not actionable")).toBeInTheDocument();
  });

  it("marks kr_pending_non_nxt rows non-actionable and shows non_nxt_venue chip", () => {
    render(
      <ProposalRow
        proposal={makeProposal({
          proposal_kind: "other",
          instrument_type: "equity_kr",
          original_payload: makeReconciliationPayload({
            reconciliation_status: "kr_pending_non_nxt",
            nxt_classification: "non_nxt_pending_ignore_for_nxt",
            nxt_eligible: false,
            warnings: ["non_nxt_venue"],
          }) as unknown as Record<string, unknown>,
        })}
        onRecordOutcome={vi.fn()}
        onRespond={vi.fn()}
      />,
    );
    expect(screen.getByText("KR broker only")).toBeInTheDocument();
    expect(screen.getByText("Non-NXT (KR broker)")).toBeInTheDocument();
    expect(screen.getByText("Non-NXT venue")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      /Non-NXT pending order/,
    );
    expect(
      screen.queryByText(/Accept records this decision only/i),
    ).not.toBeInTheDocument();
  });

  it("renders review banner for data_mismatch_requires_review", () => {
    render(
      <ProposalRow
        proposal={makeProposal({
          proposal_kind: "other",
          instrument_type: "equity_kr",
          original_payload: makeReconciliationPayload({
            reconciliation_status: "data_mismatch",
            nxt_classification: "data_mismatch_requires_review",
            nxt_eligible: true,
            warnings: ["missing_kr_universe"],
          }) as unknown as Record<string, unknown>,
        })}
        onRecordOutcome={vi.fn()}
        onRespond={vi.fn()}
      />,
    );
    expect(screen.getByText("NXT review needed")).toBeInTheDocument();
    expect(
      screen.getByText("KR universe row missing"),
    ).toBeInTheDocument();
  });

  it("does not mark actionable NXT rows as non-actionable", () => {
    render(
      <ProposalRow
        proposal={makeProposal({
          proposal_kind: "other",
          instrument_type: "equity_kr",
          original_payload: makeReconciliationPayload({
            reconciliation_status: "near_fill",
            nxt_classification: "buy_pending_actionable",
            nxt_eligible: true,
          }) as unknown as Record<string, unknown>,
        })}
        onRecordOutcome={vi.fn()}
        onRespond={vi.fn()}
      />,
    );
    expect(
      screen.queryByText(/Non-NXT pending order/),
    ).not.toBeInTheDocument();
  });
});

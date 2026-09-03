import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "../api/orderProposalApproval";
import { AccountPanelProvider } from "../desktop/AccountPanelProvider";
import { ApprovalDetailRoute, ApprovalsListRoute } from "../pages/ApprovalsRoute";
import type { OrderProposalApprovalCard } from "../types/orderProposalApproval";

const PROPOSAL_ID = "11111111-2222-4333-8444-555555555555";
const CARD: OrderProposalApprovalCard = {
  proposal_id: PROPOSAL_ID,
  market: "equity_us",
  account_mode: "kis_live",
  symbol: "AAPL",
  side: "buy",
  action: "place",
  preview: [{ quantity: "2", limit_price: "100", expected_amount: "200" }],
  rung_summary: [{ rung_index: 0, quantity: "2", limit_price: "100", expected_amount: "200", state: "pending_approval" }],
  valid_until: "2026-09-04T01:00:00Z",
  status: "pending",
  approval_hash_present: true,
  approval_channel: null,
  approved_at: null,
  recent_result: [{ rung_index: 0, state: "pending_approval", void_reason: null }],
};

function renderList() {
  return render(
    <AccountPanelProvider>
      <MemoryRouter initialEntries={["/approvals"]}>
        <Routes><Route path="/approvals" element={<ApprovalsListRoute />} /></Routes>
      </MemoryRouter>
    </AccountPanelProvider>,
  );
}

function renderDetail() {
  return render(
    <AccountPanelProvider>
      <MemoryRouter initialEntries={[`/approvals/${PROPOSAL_ID}`]}>
        <Routes><Route path="/approvals/:proposalId" element={<ApprovalDetailRoute />} /></Routes>
      </MemoryRouter>
    </AccountPanelProvider>,
  );
}

beforeEach(() => {
  vi.spyOn(api, "fetchOrderProposalApprovals").mockResolvedValue({ items: [CARD] });
  vi.spyOn(api, "fetchOrderProposalApproval").mockResolvedValue(CARD);
  vi.spyOn(api, "mutateOrderProposalApproval").mockResolvedValue({ handled: true, reason: "approved" });
});

afterEach(() => vi.restoreAllMocks());

describe("ApprovalsRoute", () => {
  it("loads the list with one request", async () => {
    renderList();
    expect(await screen.findByText("AAPL")).toBeInTheDocument();
    expect(api.fetchOrderProposalApprovals).toHaveBeenCalledTimes(1);
  });

  it("disables action buttons while the synchronous approval is running", async () => {
    let resolve!: (value: { handled: boolean; reason: string }) => void;
    vi.mocked(api.mutateOrderProposalApproval).mockReturnValue(
      new Promise((done) => { resolve = done; }),
    );
    const user = userEvent.setup();
    renderDetail();
    const approve = await screen.findByRole("button", { name: "승인" });
    await user.click(approve);
    expect(approve).toBeDisabled();
    resolve({ handled: true, reason: "approved" });
    expect(await screen.findByText("처리 결과: approved")).toBeInTheDocument();
  });

  it("polls the detail every two seconds after a processing response", async () => {
    vi.mocked(api.mutateOrderProposalApproval).mockRejectedValue(
      new api.ApprovalProcessingError(),
    );
    vi.mocked(api.fetchOrderProposalApproval)
      .mockResolvedValueOnce(CARD)
      .mockResolvedValueOnce({ ...CARD, status: "terminal" });
    renderDetail();
    await screen.findByRole("button", { name: "승인" });
    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "승인" }));
      await Promise.resolve();
    });
    expect(screen.getByText("처리 중입니다. 상태를 확인하고 있습니다.")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(api.fetchOrderProposalApproval).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });
});

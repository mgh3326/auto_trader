import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as approvalApi from "../api/lossCutApproval";
import { LossCutApprovalRoute } from "../pages/LossCutApprovalRoute";
import type {
  LossCutBeginResponse,
  LossCutConfirmResponse,
  LossCutEvidenceResponse,
} from "../types/lossCutApproval";

const PROPOSAL_ID = "11111111-2222-4333-8444-555555555555";

const EVIDENCE: LossCutEvidenceResponse = {
  mode: "proposal",
  symbol: "AAPL",
  proposal_id: PROPOSAL_ID,
  generated_at: "2026-08-14T06:00:00Z",
  can_begin: true,
  positions: [
    {
      account_ref: "masked-account",
      account_mode: "toss_live",
      market: "equity_us",
      symbol: "AAPL",
      total_quantity: "4",
      sellable_quantity: "3",
      pending_sell_quantity: "1",
      average_price: "200",
      current_price: "100",
      source: "order_preview:toss_live",
      source_status: "filled",
      source_reason: null,
      observed_at: "2026-08-14T06:00:00Z",
    },
  ],
  loss: {
    status: "filled",
    label: "손실률",
    value: { loss_pct: "-50" },
    reason: null,
    source: "order_preview:toss_live",
    as_of: "2026-08-14T06:00:00Z",
    valid_until: "2026-08-14T06:01:30Z",
  },
  reason: {
    status: "filled",
    label: "사유 판정",
    value: { exit_reason: "stop_loss" },
    reason: null,
    source: "review.trade_retrospectives",
    as_of: "2026-08-14T05:55:00Z",
    valid_until: null,
  },
  r931: {
    status: "unavailable",
    label: "R-931",
    value: null,
    reason: "no durable typed producer is registered",
    source: "not-recorded",
    as_of: null,
    valid_until: null,
  },
  consensus: {
    status: "missing",
    label: "컨센서스",
    value: null,
    reason: "no per-symbol durable snapshot",
    source: "analyst_consensus_snapshots",
    as_of: null,
    valid_until: null,
  },
  watch: {
    status: "missing",
    label: "워치 맥락",
    value: null,
    reason: "not-registered",
    source: "review.investment_watch_alerts",
    as_of: null,
    valid_until: null,
  },
  fingerprint: {
    proposal_id: PROPOSAL_ID,
    account_ref: "masked-account",
    requested_quantity: "1",
    execution: "disabled_b1",
  },
  warnings: [],
};

const BEGIN: LossCutBeginResponse = {
  proposal_id: PROPOSAL_ID,
  ceremony_id: "c".repeat(48),
  expires_at: "2026-08-14T06:01:30Z",
  evidence: EVIDENCE,
  fingerprint: EVIDENCE.fingerprint!,
  next_step: "confirm",
};

const CONFIRMED: LossCutConfirmResponse = {
  proposal_id: PROPOSAL_ID,
  status: "validated_no_execution",
  evidence: EVIDENCE,
  fingerprint: EVIDENCE.fingerprint!,
};

function renderRoute() {
  return render(
    <MemoryRouter initialEntries={[`/approvals/loss-cut/${PROPOSAL_ID}`]}>
      <Routes>
        <Route path="/approvals/loss-cut/:proposalId" element={<LossCutApprovalRoute />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.spyOn(approvalApi, "fetchLossCutProposalEvidence").mockResolvedValue(EVIDENCE);
  vi.spyOn(approvalApi, "beginLossCutApproval").mockResolvedValue(BEGIN);
  vi.spyOn(approvalApi, "confirmLossCutApproval").mockResolvedValue(CONFIRMED);
});

afterEach(() => vi.restoreAllMocks());

describe("LossCutApprovalRoute", () => {
  it("shows all five evidence fields and the exact account scope", async () => {
    renderRoute();

    await waitFor(() => expect(screen.getByText("masked-account")).toBeInTheDocument());
    for (const label of ["손실률", "사유 판정", "R-931", "컨센서스", "워치 맥락"]) {
      expect(screen.getByRole("heading", { name: label })).toBeInTheDocument();
    }
    expect(screen.getByText("4 / 3")).toBeInTheDocument();
    expect(screen.getByText("no durable typed producer is registered")).toBeInTheDocument();
  });

  it("requires two distinct clicks and returns only B1 validation", async () => {
    const user = userEvent.setup();
    renderRoute();
    const first = await screen.findByRole("button", {
      name: "1단계 · 현재 증거와 대상을 고정",
    });
    expect(
      screen.queryByRole("button", { name: "2단계 · 손절 승인 검증 확정" }),
    ).toBeNull();

    await user.click(first);
    await waitFor(() =>
      expect(approvalApi.beginLossCutApproval).toHaveBeenCalledWith(PROPOSAL_ID),
    );
    expect(approvalApi.confirmLossCutApproval).not.toHaveBeenCalled();

    const second = await screen.findByRole("button", {
      name: "2단계 · 손절 승인 검증 확정",
    });
    expect(
      screen.queryByRole("button", { name: "1단계 · 현재 증거와 대상을 고정" }),
    ).toBeNull();
    await user.click(second);

    await waitFor(() =>
      expect(approvalApi.confirmLossCutApproval).toHaveBeenCalledWith(
        PROPOSAL_ID,
        BEGIN.ceremony_id,
      ),
    );
    expect(
      await screen.findByText(
        "승인 검증이 단일 사용으로 기록됐습니다. 주문 제출은 실행되지 않았습니다.",
      ),
    ).toBeInTheDocument();
  });
});

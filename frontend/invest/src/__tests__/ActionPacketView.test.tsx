import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ActionPacketView } from "../components/investment-reports/ActionPacketView";
import type { ActionPacket } from "../types/investmentReports";

function makePacket(overrides: Partial<ActionPacket> = {}): ActionPacket {
  return {
    heldActions: [
      { verdict: "sell_review", symbol: "005930", side: "sell",
        rationale: "보유 매도 검토", itemUuid: "i1", evidenceSnapshot: {} },
      { verdict: "keep", symbol: "000660", side: null,
        rationale: "보유 유지 권장", itemUuid: "i2", evidenceSnapshot: {} },
    ],
    newBuyCandidates: [],
    noNewBuyReason: "국내 스크리너 스냅샷이 stale",
    riskReviews: [
      { verdict: "watch_only", symbol: "035720", rationale: "관망",
        itemUuid: "i3", evidenceSnapshot: {} },
    ],
    noActionReason: { kind: "data_insufficient", reasonKo: "데이터 부족",
                      blockingSources: ["portfolio"], excludedCount: 0 },
    dataGapsForNextCycle: [
      { source: "portfolio", status: "unavailable", reason: "user_id_missing" },
    ],
    ...overrides,
  };
}

describe("ActionPacketView", () => {
  it("renders the four intraday headers", () => {
    render(<ActionPacketView packet={makePacket()} />);
    expect(screen.getByRole("heading", { name: /오늘의 보유 액션/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /신규 후보/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /리스크/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /데이터 부족/ })).toBeInTheDocument();
  });

  it("renders held verdict chips with Korean labels", () => {
    render(<ActionPacketView packet={makePacket()} />);
    expect(screen.getByText("매도 검토")).toBeInTheDocument();
    expect(screen.getByText("유지")).toBeInTheDocument();
    expect(screen.getByText("005930")).toBeInTheDocument();
  });

  it("shows no-new-buy reason when there are no candidates", () => {
    render(<ActionPacketView packet={makePacket()} />);
    expect(screen.getByText(/국내 스크리너 스냅샷이 stale/)).toBeInTheDocument();
  });

  it("renders new-buy candidate priority and rank", () => {
    render(<ActionPacketView packet={makePacket({
      newBuyCandidates: [
        { verdict: "buy_review", symbol: "035420", side: "buy", rationale: "신규 후보 1순위",
          itemUuid: "i4", evidenceSnapshot: {}, priority: 1, rank: 1 },
      ],
      noNewBuyReason: null,
    })} />);
    expect(screen.getByText("1순위")).toBeInTheDocument();
    expect(screen.getByText("P1")).toBeInTheDocument();
    expect(screen.getByText("035420")).toBeInTheDocument();
  });

  it("lists data gaps with their source", () => {
    render(<ActionPacketView packet={makePacket()} />);
    expect(screen.getByText(/portfolio/)).toBeInTheDocument();
    expect(screen.getByText(/user_id_missing/)).toBeInTheDocument();
  });

  it("renders empty-state copy when a group is empty and no reason given", () => {
    render(<ActionPacketView packet={makePacket({
      heldActions: [], newBuyCandidates: [], noNewBuyReason: null,
      riskReviews: [], noActionReason: null, dataGapsForNextCycle: [],
    })} />);
    expect(screen.getAllByText("해당 없음").length).toBeGreaterThanOrEqual(3);
  });

  it("renders the reject/wait reason on risk rows", () => {
    render(<ActionPacketView packet={makePacket({
      riskReviews: [
        { verdict: "watch_only", symbol: "035720", side: null, rationale: "관망",
          itemUuid: "r1", evidenceSnapshot: {}, rejectOrWaitReason: "low_liquidity" },
      ],
    })} />);
    expect(screen.getByText("low_liquidity")).toBeInTheDocument();
  });
});


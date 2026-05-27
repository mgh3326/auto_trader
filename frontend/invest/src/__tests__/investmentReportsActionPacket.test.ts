import { describe, expect, it } from "vitest";

import { normalizeActionPacket } from "../api/investmentReports";

describe("normalizeActionPacket", () => {
  it("returns null when omitted (legacy/non-intraday)", () => {
    expect(normalizeActionPacket(undefined)).toBeNull();
    expect(normalizeActionPacket(null)).toBeNull();
  });

  it("maps snake_case payload to camelCase groups", () => {
    const packet = normalizeActionPacket({
      held_actions: [
        { verdict: "sell_review", symbol: "005930", side: "sell",
          rationale: "보유 매도 검토", item_uuid: "i1", evidence_snapshot: { x: 1 } },
        { verdict: "keep", symbol: "000660", side: null,
          rationale: "유지", item_uuid: "i2", evidence_snapshot: {} },
      ],
      new_buy_candidates: [],
      no_new_buy_reason: "스크리너 stale",
      risk_reviews: [{ verdict: "watch_only", symbol: "035720", rationale: "관망",
                       item_uuid: "i3", evidence_snapshot: {} }],
      no_action_reason: { kind: "data_insufficient", reason_ko: "데이터 부족",
                          blocking_sources: ["portfolio"], excluded_count: 2 },
      data_gaps_for_next_cycle: [
        { source: "portfolio", status: "unavailable", reason: "user_id_missing" },
      ],
    });
    expect(packet).not.toBeNull();
    expect(packet!.heldActions.map((e) => e.verdict)).toEqual(["sell_review", "keep"]);
    expect(packet!.heldActions[0]!.itemUuid).toBe("i1");
    expect(packet!.newBuyCandidates).toEqual([]);
    expect(packet!.noNewBuyReason).toBe("스크리너 stale");
    expect(packet!.riskReviews[0]!.verdict).toBe("watch_only");
    expect(packet!.noActionReason!.kind).toBe("data_insufficient");
    expect(packet!.noActionReason!.blockingSources).toEqual(["portfolio"]);
    expect(packet!.dataGapsForNextCycle[0]).toEqual({
      source: "portfolio", status: "unavailable", reason: "user_id_missing",
    });
  });
});

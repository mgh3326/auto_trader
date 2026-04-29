import { describe, expect, it } from "vitest";
import {
  KNOWN_RECON_CLASSIFICATIONS,
  KNOWN_NXT_CLASSIFICATIONS,
  parseReconciliationPayload,
} from "../api/reconciliation";

describe("parseReconciliationPayload", () => {
  it("returns null when payload is missing core fields", () => {
    expect(parseReconciliationPayload(null)).toBeNull();
    expect(parseReconciliationPayload({})).toBeNull();
  });

  it("parses a happy-path KR pending order payload", () => {
    const parsed = parseReconciliationPayload({
      advisory_only: true,
      execution_allowed: false,
      research_run_id: "11111111-1111-1111-1111-111111111111",
      candidate_kind: "pending_order",
      pending_order_id: "ORD-1",
      reconciliation_status: "near_fill",
      reconciliation_summary: "gap_within_near_fill_pct",
      nxt_classification: "buy_pending_actionable",
      nxt_eligible: true,
      venue_eligibility: { nxt: true, regular: true },
      live_quote: { price: "70200", as_of: "2026-04-29T01:00:00Z" },
      decision_support: {
        current_price: "70200",
        gap_pct: "0.2857",
        signed_distance_to_fill: "-0.2857",
        nearest_support_price: null,
        nearest_support_distance_pct: null,
        nearest_resistance_price: null,
        nearest_resistance_distance_pct: null,
        bid_ask_spread_pct: null,
      },
      warnings: ["missing_orderbook"],
    });

    expect(parsed).not.toBeNull();
    expect(parsed?.reconciliation_status).toBe("near_fill");
    expect(parsed?.nxt_classification).toBe("buy_pending_actionable");
    expect(parsed?.nxt_eligible).toBe(true);
    expect(parsed?.venue_eligibility?.nxt).toBe(true);
    expect(parsed?.warnings).toEqual(["missing_orderbook"]);
    expect(parsed?.candidate_kind).toBe("pending_order");
    expect(parsed?.live_quote?.price).toBe("70200");
  });

  it("falls back to unknown for unrecognized classifications", () => {
    const parsed = parseReconciliationPayload({
      reconciliation_status: "<script>",
      nxt_classification: "EVIL",
      candidate_kind: "pending_order",
      warnings: [],
    });
    expect(parsed?.reconciliation_status).toBe("unknown");
    expect(parsed?.nxt_classification).toBe("unknown");
  });

  it("drops warning tokens that fail the allowlist", () => {
    const parsed = parseReconciliationPayload({
      candidate_kind: "pending_order",
      reconciliation_status: "maintain",
      warnings: [
        "missing_quote",
        "<script>alert(1)</script>",
        "Non_NXT_Venue",
        "non_nxt_venue",
      ],
    });
    expect(parsed?.warnings).toEqual(["missing_quote", "non_nxt_venue"]);
  });

  it("preserves null venue eligibility entries", () => {
    const parsed = parseReconciliationPayload({
      candidate_kind: "holding",
      reconciliation_status: null,
      nxt_classification: "holding_watch_only",
      nxt_eligible: null,
      venue_eligibility: { nxt: null, regular: true },
      warnings: [],
    });
    expect(parsed?.nxt_eligible).toBeNull();
    expect(parsed?.venue_eligibility?.nxt).toBeNull();
  });

  it("KNOWN sets are non-empty and stable", () => {
    expect(KNOWN_RECON_CLASSIFICATIONS).toContain("near_fill");
    expect(KNOWN_RECON_CLASSIFICATIONS).toContain("kr_pending_non_nxt");
    expect(KNOWN_NXT_CLASSIFICATIONS).toContain("non_nxt_pending_ignore_for_nxt");
    expect(KNOWN_NXT_CLASSIFICATIONS).toContain("data_mismatch_requires_review");
  });
});

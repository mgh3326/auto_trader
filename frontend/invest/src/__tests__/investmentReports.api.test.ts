// ROB-265 Plan 5 — investmentReports API client normalization tests.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  fetchInvestmentReportBundle,
  fetchInvestmentReports,
  fetchReportSnapshotBundle,
  fetchReportSnapshotDetail,
} from "../api/investmentReports";

const originalFetch = global.fetch;

function mockFetchOnce(payload: unknown, status: number = 200): void {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  }) as unknown as typeof fetch;
}

beforeEach(() => {
  global.fetch = vi.fn();
});

afterEach(() => {
  global.fetch = originalFetch;
});

describe("fetchInvestmentReports", () => {
  it("normalises snake_case to camelCase", async () => {
    mockFetchOnce({
      reports: [
        {
          report_uuid: "uuid-1",
          report_type: "kr_morning",
          market: "kr",
          market_session: "regular",
          account_scope: "kis_mock",
          execution_mode: "mock_preview",
          created_by_profile: "test",
          title: "Test Report",
          summary: "summary",
          risk_summary: null,
          thesis_text: null,
          no_action_note: null,
          market_snapshot: {},
          portfolio_snapshot: {},
          previous_report_uuid: null,
          status: "draft",
          metadata: {},
          created_at: "2026-05-19T00:00:00Z",
          updated_at: "2026-05-19T00:00:00Z",
          published_at: null,
          valid_until: null,
        },
      ],
    });

    const result = await fetchInvestmentReports();
    expect(result.reports).toHaveLength(1);
    const report = result.reports[0]!;
    expect(report.reportUuid).toBe("uuid-1");
    expect(report.reportType).toBe("kr_morning");
    expect(report.marketSession).toBe("regular");
    expect(report.accountScope).toBe("kis_mock");
    expect(report.executionMode).toBe("mock_preview");
    expect(report.createdByProfile).toBe("test");
  });

  it("passes query params to the backend", async () => {
    mockFetchOnce({ reports: [] });
    await fetchInvestmentReports({ market: "kr", limit: 5 });
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining("market=kr"),
      expect.objectContaining({ credentials: "include" }),
    );
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining("limit=5"),
      expect.any(Object),
    );
  });

  it("throws on non-2xx response", async () => {
    mockFetchOnce({}, 500);
    await expect(fetchInvestmentReports()).rejects.toThrow(/500/);
  });
});

describe("fetchInvestmentReportBundle", () => {
  it("normalises bundle shape including delivery_status on events", async () => {
    mockFetchOnce({
      report: {
        report_uuid: "uuid-1",
        report_type: "kr_morning",
        market: "kr",
        market_session: "nxt",
        account_scope: "kis_live",
        execution_mode: "advisory_only",
        created_by_profile: "test",
        title: "Test",
        summary: "summary",
        risk_summary: null,
        thesis_text: null,
        no_action_note: null,
        market_snapshot: {},
        portfolio_snapshot: {},
        previous_report_uuid: null,
        status: "published",
        metadata: {},
        created_at: "2026-05-19T00:00:00Z",
        updated_at: "2026-05-19T00:00:00Z",
        published_at: null,
        valid_until: null,
      },
      items: [
        {
          item_uuid: "item-1",
          item_kind: "watch",
          symbol: "005930",
          side: null,
          intent: "trend_recovery_review",
          target_kind: "asset",
          priority: 10,
          confidence: null,
          rationale: "watch",
          evidence_snapshot: {},
          watch_condition: { metric: "rsi", operator: "below", threshold: 30 },
          trigger_checklist: [],
          max_action: {},
          valid_until: "2026-05-26T00:00:00Z",
          status: "activated",
          metadata: {},
          created_at: "2026-05-19T00:00:00Z",
          updated_at: "2026-05-19T00:00:00Z",
        },
      ],
      decisions_by_item_uuid: {
        "item-1": [
          {
            decision_uuid: "dec-1",
            decision: "approve",
            actor: "operator",
            decision_note: null,
            approved_payload_snapshot: null,
            created_at: "2026-05-19T00:00:00Z",
          },
        ],
      },
      alerts: [],
      events: [
        {
          event_uuid: "evt-1",
          alert_id: 42,
          source_report_uuid: "uuid-1",
          source_item_uuid: "item-1",
          market: "kr",
          target_kind: "asset",
          symbol: "005930",
          metric: "rsi",
          operator: "below",
          threshold: "30",
          threshold_key: "30",
          intent: "trend_recovery_review",
          action_mode: "notify_only",
          current_value: "25",
          scanner_snapshot: { rsi: 25 },
          outcome: "notified",
          follow_up_report_item_id: null,
          correlation_id: "corr-1",
          kst_date: "2026-05-19",
          delivery_status: "delivered",
          delivery_reason: null,
          delivered_at: "2026-05-19T00:01:00Z",
          delivery_attempts: 1,
          created_at: "2026-05-19T00:00:00Z",
        },
      ],
    });

    const bundle = await fetchInvestmentReportBundle("uuid-1");
    expect(bundle.report.reportUuid).toBe("uuid-1");
    expect(bundle.report.marketSession).toBe("nxt");
    expect(bundle.report.accountScope).toBe("kis_live");
    expect(bundle.report.executionMode).toBe("advisory_only");
    const item = bundle.items[0]!;
    expect(item.watchCondition).toEqual({
      metric: "rsi",
      operator: "below",
      threshold: 30,
    });
    const itemDecisions = bundle.decisionsByItemUuid["item-1"]!;
    expect(itemDecisions[0]!.decisionUuid).toBe("dec-1");
    const event = bundle.events[0]!;
    expect(event.deliveryStatus).toBe("delivered");
    expect(event.deliveryAttempts).toBe(1);
    expect(event.deliveredAt).toBe("2026-05-19T00:01:00Z");
  });

  it("URL-encodes the report_uuid", async () => {
    mockFetchOnce({
      report: {},
      items: [],
      decisions_by_item_uuid: {},
      alerts: [],
      events: [],
    });
    await fetchInvestmentReportBundle("uuid with space");
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining("uuid%20with%20space"),
      expect.objectContaining({ credentials: "include" }),
    );
  });

  // ROB-322 — five-section review projection normalization.
  it("normalises review_sections + no_action_summary to camelCase", async () => {
    mockFetchOnce({
      report: {},
      items: [],
      decisions_by_item_uuid: {},
      alerts: [],
      events: [],
      review_sections: {
        sections: [
          {
            key: "new_buy_candidate",
            label_ko: "신규매수 후보",
            items: [
              {
                item_uuid: "item-1",
                item_kind: "action",
                symbol: "035720",
                decision_bucket: "new_buy_candidate",
                cited_symbol_report_uuid: "sym-1",
                cited_dimension_report_uuids: ["dim-1", "dim-2"],
              },
            ],
          },
          { key: "held_strategy_review", label_ko: "보유종목 전략 변경 후보", items: [] },
        ],
        no_action_summary: {
          kind: "stale_gated",
          reason_ko: "스냅샷 stale",
          blocking_sources: ["market"],
          excluded_count: 3,
        },
      },
    });

    const bundle = await fetchInvestmentReportBundle("uuid-1");
    const review = bundle.reviewSections!;
    expect(review.sections).toHaveLength(2);
    expect(review.sections[0]!.key).toBe("new_buy_candidate");
    expect(review.sections[0]!.labelKo).toBe("신규매수 후보");
    const item = review.sections[0]!.items[0]!;
    expect(item.symbol).toBe("035720");
    expect(item.decisionBucket).toBe("new_buy_candidate");
    expect(item.citedSymbolReportUuid).toBe("sym-1");
    expect(item.citedDimensionReportUuids).toEqual(["dim-1", "dim-2"]);
    const summary = review.noActionSummary!;
    expect(summary.kind).toBe("stale_gated");
    expect(summary.reasonKo).toBe("스냅샷 stale");
    expect(summary.blockingSources).toEqual(["market"]);
    expect(summary.excludedCount).toBe(3);
  });

  it("returns null reviewSections when the field is absent (legacy)", async () => {
    mockFetchOnce({
      report: {},
      items: [],
      decisions_by_item_uuid: {},
      alerts: [],
      events: [],
    });
    const bundle = await fetchInvestmentReportBundle("uuid-1");
    expect(bundle.reviewSections).toBeNull();
  });

  describe("fetchInvestmentReportBundle linked orders (ROB-554)", () => {
    it("maps item.linked_orders snake_case to camelCase linkedOrders", async () => {
      mockFetchOnce({
        report: { report_uuid: "uuid-1" },
        items: [
          {
            item_uuid: "item-1",
            rationale: "r",
            linked_orders: [
              {
                broker: "upbit",
                account_scope: "upbit_live",
                market: "crypto",
                order_no: "df98c030-abc",
                ledger_id: 7,
                symbol: "BTC",
                side: "buy",
                status: "filled",
                filled_qty: "0.01",
                avg_fill_price: "96180000",
                order_time: "2026-06-12T05:03:00Z",
                report_item_uuid: "item-1",
              },
            ],
          },
        ],
        decisions_by_item_uuid: {},
        alerts: [],
        events: [],
      });

      const bundle = await fetchInvestmentReportBundle("uuid-1");
      const linked = bundle.items[0]!.linkedOrders!;
      expect(linked).not.toBeNull();
      expect(linked[0]!.orderNo).toBe("df98c030-abc");
      expect(linked[0]!.market).toBe("crypto");
      expect(linked[0]!.ledgerId).toBe(7);
      expect(linked[0]!.status).toBe("filled");
    });

    it("maps absent linked_orders to null", async () => {
      mockFetchOnce({
        report: { report_uuid: "uuid-2" },
        items: [{ item_uuid: "item-2", rationale: "r" }],
        decisions_by_item_uuid: {},
        alerts: [],
        events: [],
      });
      const bundle = await fetchInvestmentReportBundle("uuid-2");
      expect(bundle.items[0]!.linkedOrders).toBeNull();
    });
  });
});

describe("fetchReportSnapshotBundle", () => {
  it("normalises the bundle list response", async () => {
    mockFetchOnce({
      bundle: {
        bundle_uuid: "bundle-1",
        purpose: "rob275_smoke",
        market: "kr",
        account_scope: "kis_live",
        policy_version: "intraday_action_report_v1",
        status: "partial",
        as_of: "2026-05-20T11:00:00Z",
        coverage_summary: { portfolio: { count: 1 } },
        freshness_summary: { overall: "partial" },
        created_at: "2026-05-20T11:00:00Z",
      },
      items: [
        {
          snapshot_uuid: "snap-1",
          role: "required",
          snapshot_kind: "portfolio",
          source_kind: "manual",
          market: "kr",
          symbol: null,
          account_scope: "kis_live",
          freshness_status: "fresh",
          as_of: "2026-05-20T11:00:00Z",
          valid_until: null,
          source_table: "market_quote_snapshots",
          source_id: 42,
          source_uri: "market_quote_snapshots:abc",
          payload_size_bytes: 256,
        },
      ],
      unavailable_sources: { naver_remote_debug: "blocked" },
      source_conflicts: null,
      legacy_no_snapshot: false,
    });

    const response = await fetchReportSnapshotBundle("uuid-1");
    expect(response.legacyNoSnapshot).toBe(false);
    expect(response.bundle?.bundleUuid).toBe("bundle-1");
    expect(response.bundle?.market).toBe("kr");
    expect(response.bundle?.accountScope).toBe("kis_live");
    expect(response.items).toHaveLength(1);
    expect(response.items[0]!.snapshotUuid).toBe("snap-1");
    expect(response.items[0]!.payloadSizeBytes).toBe(256);
    expect(response.items[0]!.freshnessStatus).toBe("fresh");
    expect(response.items[0]!.sourceId).toBe(42);
    expect(response.items[0]!.sourceTable).toBe("market_quote_snapshots");
    expect(response.items[0]!.sourceUri).toBe("market_quote_snapshots:abc");
    expect(response.unavailableSources).toEqual({
      naver_remote_debug: "blocked",
    });
    expect(response.sourceConflicts).toBeNull();
  });

  it("normalises a legacy/no-snapshot response", async () => {
    mockFetchOnce({
      bundle: null,
      items: [],
      unavailable_sources: null,
      source_conflicts: null,
      legacy_no_snapshot: true,
    });
    const response = await fetchReportSnapshotBundle("uuid-1");
    expect(response.legacyNoSnapshot).toBe(true);
    expect(response.bundle).toBeNull();
    expect(response.items).toEqual([]);
  });
});

describe("fetchReportSnapshotDetail", () => {
  it("normalises the detail payload and URL-encodes both UUIDs", async () => {
    mockFetchOnce({
      snapshot_uuid: "snap-1",
      role: "required",
      snapshot_kind: "portfolio",
      source_kind: "manual",
      market: "kr",
      symbol: null,
      account_scope: "kis_live",
      source_table: null,
      source_id: null,
      source_uri: null,
      freshness_status: "fresh",
      as_of: "2026-05-20T11:00:00Z",
      valid_until: null,
      source_timestamps_json: { collected_at: "2026-05-20T11:00:00Z" },
      coverage_json: {},
      errors_json: {},
      payload_json: { cash_krw: 1_000_000 },
    });

    const detail = await fetchReportSnapshotDetail("uuid 1", "snap 1");
    expect(detail.snapshotUuid).toBe("snap-1");
    expect(detail.role).toBe("required");
    expect(detail.payloadJson).toEqual({ cash_krw: 1_000_000 });
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining("uuid%201/snapshots/snap%201"),
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("throws on non-2xx (e.g. 404 for non-member snapshot)", async () => {
    mockFetchOnce({}, 404);
    await expect(
      fetchReportSnapshotDetail("uuid-1", "snap-x"),
    ).rejects.toThrow(/404/);
  });
});

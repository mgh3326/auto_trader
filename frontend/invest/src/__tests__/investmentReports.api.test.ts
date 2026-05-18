// ROB-265 Plan 5 — investmentReports API client normalization tests.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  fetchInvestmentReportBundle,
  fetchInvestmentReports,
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
    expect(result.reports[0].reportUuid).toBe("uuid-1");
    expect(result.reports[0].reportType).toBe("kr_morning");
    expect(result.reports[0].marketSession).toBe("regular");
    expect(result.reports[0].accountScope).toBe("kis_mock");
    expect(result.reports[0].executionMode).toBe("mock_preview");
    expect(result.reports[0].createdByProfile).toBe("test");
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
    expect(bundle.items[0].watchCondition).toEqual({
      metric: "rsi",
      operator: "below",
      threshold: 30,
    });
    expect(bundle.decisionsByItemUuid["item-1"][0].decisionUuid).toBe("dec-1");
    expect(bundle.events[0].deliveryStatus).toBe("delivered");
    expect(bundle.events[0].deliveryAttempts).toBe(1);
    expect(bundle.events[0].deliveredAt).toBe("2026-05-19T00:01:00Z");
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
});

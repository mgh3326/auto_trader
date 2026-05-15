import { afterEach, expect, test, vi } from "vitest";

import { fetchActionCenterCandidates, fetchActionCenterReport, fetchActionCenterReports } from "../api/actionCenter";

const REPORTS_PAYLOAD = {
  reports: [
    {
      reportUuid: "report-1",
      reportType: "daily",
      market: "kr",
      accountScope: "kis-live",
      createdByProfile: "analyst",
      status: "published",
      summary: "오늘의 후보 요약",
      riskSummary: "정규장 확인 필요",
      dataFreshness: { accountFeasibility: "확인 불가" },
      coverage: { liquidity: "degraded" },
      sourcePolicy: [],
      safetyNotes: ["주문 실행이 아닙니다."],
      createdAt: "2026-05-15T00:00:00Z",
      publishedAt: "2026-05-15T00:01:00Z",
      validUntil: null,
      stageResults: [],
      candidates: [],
    },
  ],
  unavailableLabel: "확인 불가",
};

const CANDIDATES_PAYLOAD = {
  candidates: [
    {
      candidateUuid: "cand-1",
      reportUuid: "report-1",
      symbol: "005930",
      market: "kr",
      side: "buy",
      actionType: "buy_candidate",
      priority: 10,
      confidence: 0.72,
      quantity: null,
      quantityPct: null,
      notional: null,
      limitPrice: null,
      currency: null,
      thesis: "실적 모멘텀",
      riskNotes: ["뉴스 리스크 확인 필요"],
      verification: { accountFeasibility: "확인 불가" },
      blockingReasons: [],
      approvalStatus: "awaiting_approval",
      approvalType: "manual",
      executionState: "not_submitted",
      createdAt: "2026-05-15T00:02:00Z",
      validUntil: null,
    },
  ],
  unavailableLabel: "확인 불가",
};

afterEach(() => {
  vi.restoreAllMocks();
});

test("fetchActionCenterReports reads the non-executing invest action center report endpoint", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(REPORTS_PAYLOAD), { status: 200, headers: { "content-type": "application/json" } }),
  );

  await expect(fetchActionCenterReports()).resolves.toEqual(REPORTS_PAYLOAD);
  expect(fetchMock).toHaveBeenCalledWith("/invest/api/action-center/reports", {
    credentials: "include",
    signal: undefined,
  });
});

test("fetchActionCenterReport reads one report by uuid without mutating approval or orders", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(REPORTS_PAYLOAD.reports[0]), { status: 200, headers: { "content-type": "application/json" } }),
  );

  await expect(fetchActionCenterReport("report-1")).resolves.toEqual(REPORTS_PAYLOAD.reports[0]);
  expect(fetchMock).toHaveBeenCalledWith("/invest/api/action-center/reports/report-1", {
    credentials: "include",
    signal: undefined,
  });
});

test("fetchActionCenterCandidates reads the non-executing candidate queue endpoint", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(CANDIDATES_PAYLOAD), { status: 200, headers: { "content-type": "application/json" } }),
  );

  await expect(fetchActionCenterCandidates()).resolves.toEqual(CANDIDATES_PAYLOAD);
  expect(fetchMock).toHaveBeenCalledWith("/invest/api/action-center/candidates", {
    credentials: "include",
    signal: undefined,
  });
});

test("action center API normalizes backend items and snake_case fields", async () => {
  vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(new Response(JSON.stringify({ count: 1, items: [{
      report_uuid: "report-2",
      report_type: "daily",
      market: "kr",
      account_scope: null,
      created_by_profile: "analyst",
      status: "published",
      summary: "요약",
      risk_summary: null,
      data_freshness: {},
      coverage: {},
      source_policy: ["KIS live authority"],
      safety_notes: ["주문 실행이 아닙니다."],
      created_at: "2026-05-15T00:00:00Z",
      published_at: null,
      valid_until: null,
      stages: [{ stage_key: "account", source: "kis", status: "unavailable", freshness_at: null, unavailable_reason: "확인 불가", warnings: [] }],
      candidates: [],
    }] }), { status: 200, headers: { "content-type": "application/json" } }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ count: 1, items: [{
      candidate_uuid: "cand-2",
      report_uuid: "report-2",
      symbol: "005930",
      market: "kr",
      side: "buy",
      action_type: "buy_candidate",
      priority: 1,
      thesis: "테스트",
      risk_notes: ["확인 필요"],
      verification: {},
      blocking_reasons: [],
      approval_status: "awaiting_approval",
      approval_type: "manual",
      execution_state: "not_submitted",
      created_at: "2026-05-15T00:02:00Z",
    }] }), { status: 200, headers: { "content-type": "application/json" } }));

  await expect(fetchActionCenterReports()).resolves.toMatchObject({
    reports: [{ reportUuid: "report-2", sourcePolicy: ["KIS live authority"], stageResults: [{ stageKey: "account", unavailableReason: "확인 불가" }] }],
    unavailableLabel: "확인 불가",
  });
  await expect(fetchActionCenterCandidates()).resolves.toMatchObject({
    candidates: [{ candidateUuid: "cand-2", actionType: "buy_candidate", riskNotes: ["확인 필요"], blockingReasons: [] }],
    unavailableLabel: "확인 불가",
  });
});

test("action center fetch errors include only the endpoint and status", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("broker token should not echo", { status: 503 }));

  await expect(fetchActionCenterCandidates()).rejects.toThrow("/invest/api/action-center/candidates 503");
});

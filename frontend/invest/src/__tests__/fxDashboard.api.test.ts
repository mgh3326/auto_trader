import { afterEach, expect, test, vi } from "vitest";

import { fetchFxDashboard } from "../api/fxDashboard";

const PAYLOAD = {
  asOf: "2026-05-13T03:00:00Z",
  dataState: "fresh",
  warnings: [],
  disclaimers: [],
  sourceFreshness: [],
  usdKrw: { symbol: "FX_USDKRW", label: "USD/KRW", value: 1450.25, spot: 1450.25, change: 2.1, changePct: 0.15, tone: "up", updatedAt: "2026-05-13T03:00:00Z", dataState: "fresh", source: "naver" },
  thresholds: [],
  defenseSignal: { state: "watch", score: 35, confidence: "medium", labelKo: "경계", summaryKo: "방어성 호가 가능성은 참고 신호입니다.", reasonsKo: [], evidence: [], notConfirmedIntervention: true, needsAfterVerification: true },
  globalDollar: [],
  krwCrosses: [],
  foreignFlow: { dataState: "missing", summaryKo: "수급 데이터 없음", items: [] },
  news: { dataState: "missing", items: [], warning: "뉴스 데이터 없음" },
  events: { dataState: "missing", items: [], warning: "일정 데이터 없음" },
  afterVerification: { dataState: "missing", officialEvidence: [], dealerEvidence: [], ndfEvidence: [], summaryKo: "사후 검증 필요" },
};

afterEach(() => {
  vi.restoreAllMocks();
});

test("fetchFxDashboard reads the read-only FX dashboard endpoint", async () => {
  const controller = new AbortController();
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(PAYLOAD), { status: 200, headers: { "content-type": "application/json" } }),
  );

  await expect(fetchFxDashboard(controller.signal)).resolves.toEqual(PAYLOAD);
  expect(fetchMock).toHaveBeenCalledWith("/invest/api/market/fx/dashboard", {
    credentials: "include",
    signal: controller.signal,
  });
});

test("fetchFxDashboard raises a scrubbed endpoint/status error", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("nope", { status: 503 }));

  await expect(fetchFxDashboard()).rejects.toThrow("/invest/api/market/fx/dashboard 503");
});

import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import { FxMacroRoute } from "../pages/desktop/FxMacroPage";
import * as fxApi from "../api/fxDashboard";
import type { FxDashboardResponse } from "../types/fxDashboard";

const FX_PAYLOAD: FxDashboardResponse = {
  asOf: "2026-05-13T03:00:00Z",
  dataState: "partial",
  warnings: ["global_dollar: partial"],
  disclaimers: [
    { code: "not_confirmed_intervention", severity: "caution", textKo: "당국 개입은 확인되지 않았으며 방어성 호가/경계 신호로만 봅니다." },
  ],
  sourceFreshness: [
    { source: "naver_market_index", label: "Naver USD/KRW", dataState: "fresh", updatedAt: "2026-05-13T03:00:00Z", staleAfterMinutes: 20, warning: null },
  ],
  usdKrw: {
    symbol: "FX_USDKRW",
    label: "USD/KRW",
    value: 1450.25,
    spot: 1450.25,
    change: 2.1,
    changePct: 0.15,
    tone: "up",
    updatedAt: "2026-05-13T03:00:00Z",
    dataState: "fresh",
    source: "naver",
  },
  thresholds: [
    { level: 1450, label: "1450원 경계", distancePct: 0.02, state: "near" },
  ],
  defenseSignal: {
    state: "watch",
    score: 42,
    confidence: "medium",
    labelKo: "당국 경계 가능성",
    summaryKo: "1450원 부근 방어성 호가 가능성은 사후 확인이 필요합니다.",
    reasonsKo: ["원화 약세와 달러 강세가 동시에 관찰됩니다."],
    evidence: [
      { kind: "quote", labelKo: "USD/KRW", value: "1450.25", source: "naver", dataState: "fresh" },
    ],
    notConfirmedIntervention: true,
    needsAfterVerification: true,
  },
  globalDollar: [
    { symbol: "DX-Y.NYB", label: "DXY", value: 105.2, changePct: 0.22, dataState: "partial", source: "yahoo" },
  ],
  krwCrosses: [
    { symbol: "FX_CNYKRW", label: "CNY/KRW", value: 200.1, changePct: -0.1, dataState: "fresh", source: "naver" },
  ],
  foreignFlow: { dataState: "missing", summaryKo: "외국인 수급은 아직 연결되지 않았습니다.", items: [] },
  news: { dataState: "partial", items: [], warning: "FX 뉴스 연결 대기" },
  events: { dataState: "missing", items: [], warning: "매크로 일정 연결 대기" },
  afterVerification: { dataState: "missing", officialEvidence: [], dealerEvidence: [], ndfEvidence: [], summaryKo: "공식/딜러/NDF 사후 검증은 아직 필요합니다." },
};

function wrap(ui: React.ReactElement) {
  return <MemoryRouter basename="/invest" initialEntries={["/invest/market/fx"]}>{ui}</MemoryRouter>;
}

beforeEach(() => {
  Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 1280 });
  vi.spyOn(fxApi, "fetchFxDashboard").mockResolvedValue(FX_PAYLOAD);
});

test("renders FX macro detail page with cautious read-only state", async () => {
  render(wrap(<FxMacroRoute />));

  await waitFor(() => expect(screen.getByText("1,450.25")).toBeInTheDocument());
  expect(screen.getByRole("heading", { name: "FX·매크로" })).toBeInTheDocument();
  expect(screen.getByText("당국 경계 가능성")).toBeInTheDocument();
  expect(screen.getByText(/1450원 부근 방어성 호가 가능성/)).toBeInTheDocument();
  expect(screen.getByText(/당국 개입은 확인되지 않았으며/)).toBeInTheDocument();
  expect(screen.getByText("Naver USD/KRW")).toBeInTheDocument();
  expect(screen.getByText(/주문·매매 API, watch\/order intent, scheduler activation을 호출하지 않습니다/)).toBeInTheDocument();

  [
    ["개입", "확정"],
    ["정부", "개입", "확정"],
    ["당국", "개입", "확정"],
    ["정부가", "방어"],
    ["당국이", "방어"],
  ]
    .map((parts) => parts.join(" "))
    .forEach((phrase) => {
      expect(screen.queryByText(phrase)).not.toBeInTheDocument();
    });
});

test("shows friendly FX error without raw endpoint or status", async () => {
  vi.spyOn(fxApi, "fetchFxDashboard").mockRejectedValue(new Error("/invest/api/market/fx/dashboard 503"));

  render(wrap(<FxMacroRoute />));

  await waitFor(() => expect(screen.getByText(/FX·매크로 데이터를 일시적으로 불러오지 못했습니다/)).toBeInTheDocument());
  expect(screen.queryByText(/\/invest\/api\/market\/fx\/dashboard/)).not.toBeInTheDocument();
  expect(screen.queryByText(/503/)).not.toBeInTheDocument();
});

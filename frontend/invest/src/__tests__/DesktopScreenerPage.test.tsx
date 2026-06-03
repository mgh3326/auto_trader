import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, beforeEach, test, expect } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { DesktopScreenerPage } from "../pages/desktop/DesktopScreenerPage";
import { AccountPanelProvider } from "../desktop/AccountPanelProvider";
import * as screenerApi from "../api/screener";
import * as panelApi from "../api/accountPanel";
import * as signalsApi from "../api/signals";

const PRESETS = {
  presets: [
    {
      id: "consecutive_gainers", name: "연속 상승세",
      description: "일주일 연속 상승세를 보이는 주식",
      badges: ["인기"],
      filterChips: [{ label: "주가등락률", detail: "1주일 전 보다 · 0% 이상" }],
      metricLabel: "주가등락률", market: "kr" as const,
    },
    {
      id: "cheap_value", name: "아직 저렴한 가치주",
      description: "PER, PBR 모두 낮은 저평가 종목",
      badges: [],
      filterChips: [{ label: "PER", detail: "15 이하" }],
      metricLabel: "PER", market: "kr" as const,
    },
    {
      id: "investor_flow_momentum", name: "수급 모멘텀",
      description: "외국인 연속 순매수 흐름이 강한 종목 (스냅샷 기반)",
      badges: ["MVP"],
      filterChips: [{ label: "투자자별 수급", detail: "외국인 3일+ 연속 순매수" }],
      metricLabel: "외국인 순매수", market: "kr" as const,
    },
    {
      id: "double_buy", name: "쌍끌이 매수",
      description: "기관과 외국인이 동시에 매수하는 종목",
      badges: ["NEW"],
      filterChips: [
        { label: "국내", detail: null },
        { label: "외국인", detail: "순매수" },
        { label: "기관", detail: "순매수" },
        { label: "주가등락률", detail: "1일 ≥ 0%" },
        { label: "데이터", detail: "지연 스냅샷 기반" },
      ],
      metricLabel: "주가등락률", market: "kr" as const,
    },
  ],
  selectedPresetId: "consecutive_gainers",
};

const ROW = {
  rank: 1, symbol: "005930", market: "kr" as const, name: "삼성전자",
  logoUrl: null, isWatched: true,
  priceLabel: "80,000원", changePctLabel: "+1.23%", changeAmountLabel: "+970원",
  changeDirection: "up" as const, category: "반도체",
  marketCapLabel: "478조원", volumeLabel: "12,345,678",
  analystLabel: "구매", metricValueLabel: "+8.00%", investorFlowChip: null, warnings: [],
};

const RESULTS_GAINERS = {
  presetId: "consecutive_gainers", title: "연속 상승세",
  description: "일주일 연속 상승세를 보이는 주식",
  filterChips: [{ label: "주가등락률", detail: "1주일 전 보다 · 0% 이상" }],
  metricLabel: "주가등락률", results: [ROW], warnings: [],
  freshness: {
    fetchedAt: "2026-05-10T05:30:00+00:00",
    asOfLabel: "2026.05.10 14:30 기준",
    relativeLabel: "방금 갱신",
    cacheHit: false,
    source: "live" as const,
    dataState: "fresh" as const,
  },
};

const RESULTS_VALUE = {
  ...RESULTS_GAINERS,
  presetId: "cheap_value", title: "아직 저렴한 가치주",
  description: "PER, PBR 모두 낮은 저평가 종목",
  metricLabel: "PER",
  filterChips: [{ label: "PER", detail: "15 이하" }],
  results: [{ ...ROW, metricValueLabel: "14.0" }],
};

const RESULTS_INVESTOR_FLOW = {
  ...RESULTS_GAINERS,
  presetId: "investor_flow_momentum", title: "수급 모멘텀",
  description: "외국인 연속 순매수 흐름이 강한 종목 (스냅샷 기반)",
  metricLabel: "외국인 순매수",
  filterChips: [{ label: "투자자별 수급", detail: "외국인 3일+ 연속 순매수" }],
  results: [{
    ...ROW,
    symbol: "403550",
    name: "에스케이엔펄스",
    metricValueLabel: "+20,859주",
    investorFlowChip: {
      label: "외국인 4일 순매수",
      tone: "foreign_buy" as const,
      dataState: "fresh" as const,
      snapshotDate: "2026-05-13",
    },
  }],
};

const CRYPTO_PRESETS = {
  presets: [
    {
      id: "crypto_high_volume", name: "거래대금 상위 코인",
      description: "Upbit 거래대금이 큰 가상자산",
      badges: ["가상자산"],
      filterChips: [{ label: "거래대금", detail: "24시간 상위" }],
      metricLabel: "거래대금", market: "crypto" as const,
    },
  ],
  selectedPresetId: "crypto_high_volume",
};

const CRYPTO_RESULTS = {
  ...RESULTS_GAINERS,
  presetId: "crypto_high_volume", title: "거래대금 상위 코인",
  description: "Upbit 거래대금이 큰 가상자산",
  filterChips: [{ label: "거래대금", detail: "24시간 상위" }],
  metricLabel: "거래대금",
  results: [{
    ...ROW,
    symbol: "KRW-BTC",
    market: "crypto" as const,
    name: "Bitcoin",
    priceLabel: "150,000,000원",
    marketCapLabel: "$2.10T",
    category: "Crypto",
    metricValueLabel: "120,000,000,000",
    sourceContext: [
      { source: "snapshot_cache" as const, label: "스냅샷 캐시", state: "cached" as const, fetchedAt: null, detail: null },
      { source: "tvscreener_upbit" as const, label: "TV Screener Upbit", state: "supported" as const, fetchedAt: null, detail: null },
    ],
    riskContext: [
      { kind: "low_rsi", label: "RSI 31.5 저점권", severity: "info" as const, source: "tvscreener_upbit" as const },
    ],
    candidateContext: {
      scoreLabel: "거래대금 120,000,000,000",
      reasons: ["24시간 KRW 거래대금 상위"],
      source: "tvscreener_upbit" as const,
    },
  }],
  sources: [
    { source: "snapshot_cache" as const, label: "스냅샷 캐시", state: "cached" as const, fetchedAt: null, detail: null },
  ],
};

function wrap(ui: React.ReactElement) {
  return (
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={["/invest/screener"]}>
        {ui}
      </MemoryRouter>
    </AccountPanelProvider>
  );
}

beforeEach(() => {
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue({
    homeSummary: { includedSources: [], excludedSources: [], totalValueKrw: 0 },
    accounts: [], groupedHoldings: [], watchSymbols: [], sourceVisuals: [],
    meta: { warnings: [], watchlistAvailable: true },
  });
  vi.spyOn(signalsApi, "fetchSignals").mockResolvedValue({
    tab: "kr", asOf: new Date().toISOString(), items: [], meta: { warnings: [] },
  });
  vi.spyOn(screenerApi, "fetchScreenerPresets").mockImplementation(async (market = "kr") => {
    return market === "crypto" ? CRYPTO_PRESETS : PRESETS;
  });
  vi.spyOn(screenerApi, "fetchScreenerResults").mockImplementation(async (id: string, market = "kr") => {
    if (market === "crypto") {
      return CRYPTO_RESULTS;
    }
    if (market === "us") {
      return {
        ...RESULTS_VALUE,
        title: "미국 가치주",
        results: [{
          ...ROW,
          symbol: "AAPL",
          market: "us" as const,
          name: "Apple Inc.",
          priceLabel: "$210.40",
          marketCapLabel: "$3.20T",
          category: "Technology",
        }],
      };
    }
    if (id === "cheap_value") return RESULTS_VALUE;
    if (id === "investor_flow_momentum") return RESULTS_INVESTOR_FLOW;
    return RESULTS_GAINERS;
  });
});

test("renders the default preset and switches when another preset is clicked", async () => {
  render(wrap(<DesktopScreenerPage />));
  await waitFor(() => expect(screen.getByText("삼성전자")).toBeInTheDocument());
  expect(await screen.findByTestId("screener-freshness")).toHaveTextContent(
    "2026.05.10 14:30 기준 · 방금 갱신",
  );

  await userEvent.click(screen.getByTestId("screener-preset-cheap_value"));
  await waitFor(() =>
    expect(screen.getByText("PER, PBR 모두 낮은 저평가 종목")).toBeInTheDocument(),
  );
  expect(screenerApi.fetchScreenerResults).toHaveBeenCalledWith("consecutive_gainers", "kr");
  expect(screenerApi.fetchScreenerResults).toHaveBeenCalledWith("cheap_value", "kr");
});

test("shows an empty-state message when results are empty", async () => {
  vi.spyOn(screenerApi, "fetchScreenerResults").mockResolvedValue({
    ...RESULTS_GAINERS, results: [],
  });
  render(wrap(<DesktopScreenerPage />));
  await waitFor(() =>
    expect(screen.getByText(/표시할 종목이 없습니다/)).toBeInTheDocument(),
  );
});


test("renders the investor-flow MVP preset and result chip", async () => {
  render(wrap(<DesktopScreenerPage />));
  await waitFor(() => expect(screen.getByText("삼성전자")).toBeInTheDocument());

  await userEvent.click(screen.getByTestId("screener-preset-investor_flow_momentum"));

  await waitFor(() => expect(screen.getByText("에스케이엔펄스")).toBeInTheDocument());
  expect(screen.getByText("외국인 연속 순매수 흐름이 강한 종목 (스냅샷 기반)")).toBeInTheDocument();
  expect(screen.getByText("외국인 4일 순매수")).toBeInTheDocument();
  expect(screenerApi.fetchScreenerResults).toHaveBeenCalledWith("investor_flow_momentum", "kr");
});


test("shows a friendly message when screener results fail", async () => {
  vi.spyOn(screenerApi, "fetchScreenerResults").mockRejectedValue(
    new Error("screener/results 500"),
  );

  render(wrap(<DesktopScreenerPage />));

  await waitFor(() =>
    expect(screen.getByText(/스크리너 데이터를 일시적으로 불러오지 못했습니다/)).toBeInTheDocument(),
  );
  expect(screen.queryByText(/screener\/results 500/)).not.toBeInTheDocument();
});


test("switches to the US market", async () => {
  render(wrap(<DesktopScreenerPage />));
  await waitFor(() => expect(screen.getByText("삼성전자")).toBeInTheDocument());

  await userEvent.click(screen.getByRole("button", { name: "미국" }));

  await waitFor(() => expect(screen.getByText("Apple Inc.")).toBeInTheDocument());
  expect(screenerApi.fetchScreenerResults).toHaveBeenCalledWith("consecutive_gainers", "us");
});


test("switches to the crypto market", async () => {
  render(wrap(<DesktopScreenerPage />));
  await waitFor(() => expect(screen.getByText("삼성전자")).toBeInTheDocument());

  await userEvent.click(screen.getByRole("button", { name: "가상자산" }));

  await waitFor(() => expect(screen.getByText("Bitcoin")).toBeInTheDocument());
  expect(screen.getByText("스냅샷 캐시")).toBeInTheDocument();
  expect(screen.getByText("TV Screener Upbit")).toBeInTheDocument();
  expect(screen.getByText("RSI 31.5 저점권")).toBeInTheDocument();
  expect(screen.getByText("24시간 KRW 거래대금 상위")).toBeInTheDocument();
  expect(screenerApi.fetchScreenerPresets).toHaveBeenCalledWith("crypto");
  expect(screenerApi.fetchScreenerResults).toHaveBeenCalledWith("crypto_high_volume", "crypto");
});


test("renders the coverage degraded empty-state when the partition is thin", async () => {
  vi.spyOn(screenerApi, "fetchScreenerResults").mockResolvedValue({
    ...RESULTS_GAINERS,
    results: [],
    freshness: {
      ...RESULTS_GAINERS.freshness,
      primary: {
        kind: "screener_snapshot" as const,
        snapshotDate: "2026-05-22",
        computedAt: null,
        asOfLabel: "2026.05.22 15:30 기준",
        dataState: "stale" as const,
        source: "invest_screener_snapshots",
        degradationReason: "coverage_below_floor" as const,
        coverageLabel: "20 / 3,800 (0.5%)",
      },
    },
  });
  render(wrap(<DesktopScreenerPage />));
  await waitFor(() => {
    expect(
      screen.getAllByText(/20 \/ 3,800 \(0\.5%\)/).length,
    ).toBeGreaterThan(0);
  });
});


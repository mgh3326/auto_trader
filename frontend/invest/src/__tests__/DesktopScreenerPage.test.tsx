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
      metricLabel: "연속상승", market: "kr" as const,
    },
    {
      id: "cheap_value", name: "아직 저렴한 가치주",
      description: "PER, PBR 모두 낮은 저평가 종목",
      badges: [],
      filterChips: [{ label: "PER", detail: "15 이하" }],
      metricLabel: "PER", market: "kr" as const,
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
  analystLabel: "구매", metricValueLabel: "5일", warnings: [],
};

const RESULTS_GAINERS = {
  presetId: "consecutive_gainers", title: "연속 상승세",
  description: "일주일 연속 상승세를 보이는 주식",
  filterChips: [{ label: "주가등락률", detail: "1주일 전 보다 · 0% 이상" }],
  metricLabel: "연속상승", results: [ROW], warnings: [],
};

const RESULTS_VALUE = {
  ...RESULTS_GAINERS,
  presetId: "cheap_value", title: "아직 저렴한 가치주",
  description: "PER, PBR 모두 낮은 저평가 종목",
  metricLabel: "PER",
  filterChips: [{ label: "PER", detail: "15 이하" }],
  results: [{ ...ROW, metricValueLabel: "14.0" }],
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
  vi.spyOn(screenerApi, "fetchScreenerPresets").mockResolvedValue(PRESETS);
  vi.spyOn(screenerApi, "fetchScreenerResults").mockImplementation(async (id: string, market = "kr") => {
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
    return id === "cheap_value" ? RESULTS_VALUE : RESULTS_GAINERS;
  });
});

test("renders the default preset and switches when another preset is clicked", async () => {
  render(wrap(<DesktopScreenerPage />));
  await waitFor(() => expect(screen.getByText("삼성전자")).toBeInTheDocument());

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


test("switches to the US market", async () => {
  render(wrap(<DesktopScreenerPage />));
  await waitFor(() => expect(screen.getByText("삼성전자")).toBeInTheDocument());

  await userEvent.click(screen.getByRole("button", { name: "미국" }));

  await waitFor(() => expect(screen.getByText("Apple Inc.")).toBeInTheDocument());
  expect(screenerApi.fetchScreenerResults).toHaveBeenCalledWith("consecutive_gainers", "us");
});

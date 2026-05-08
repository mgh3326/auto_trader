import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, beforeEach, test, expect } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { DesktopScreenerPage } from "../pages/desktop/DesktopScreenerPage";
import * as screenerApi from "../api/screener";
import * as panelApi from "../api/accountPanel";

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
  ],
  selectedPresetId: "consecutive_gainers",
};

const ROW = {
  rank: 1, symbol: "005930", market: "kr" as const, name: "삼성전자",
  logoUrl: null, isWatched: true,
  priceLabel: "80,000원", changePctLabel: "+1.23%", changeAmountLabel: "+970원",
  changeDirection: "up" as const, category: "반도체",
  marketCapLabel: "478조원", volumeLabel: "12,345,678",
  analystLabel: "구매", metricValueLabel: "+1.23%", warnings: [],
};

const RESULTS_GAINERS = {
  presetId: "consecutive_gainers", title: "연속 상승세",
  description: "일주일 연속 상승세를 보이는 주식",
  filterChips: [{ label: "주가등락률", detail: "1주일 전 보다 · 0% 이상" }],
  metricLabel: "주가등락률", results: [ROW], warnings: [],
};

const RESULTS_VALUE = {
  ...RESULTS_GAINERS,
  presetId: "cheap_value", title: "아직 저렴한 가치주",
  description: "PER, PBR 모두 낮은 저평가 종목",
  metricLabel: "PER",
  filterChips: [{ label: "PER", detail: "15 이하" }],
  results: [{ ...ROW, metricValueLabel: "14.0" }],
};

beforeEach(() => {
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue({
    homeSummary: { includedSources: [], excludedSources: [], totalValueKrw: 0 },
    accounts: [], groupedHoldings: [], watchSymbols: [], sourceVisuals: [],
    meta: { warnings: [], watchlistAvailable: true },
  });
  vi.spyOn(screenerApi, "fetchScreenerPresets").mockResolvedValue(PRESETS);
  vi.spyOn(screenerApi, "fetchScreenerResults").mockImplementation(async (id: string) =>
    id === "cheap_value" ? RESULTS_VALUE : RESULTS_GAINERS,
  );
});

test("renders the default preset and switches when another preset is clicked", async () => {
  render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/screener"]}>
      <DesktopScreenerPage />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getByText("연속 상승세")).toBeInTheDocument());
  expect(screen.getByText("삼성전자")).toBeInTheDocument();

  await userEvent.click(screen.getByTestId("screener-preset-cheap_value"));
  await waitFor(() =>
    expect(screen.getByText("PER, PBR 모두 낮은 저평가 종목")).toBeInTheDocument(),
  );
  expect(screenerApi.fetchScreenerResults).toHaveBeenCalledWith("consecutive_gainers");
  expect(screenerApi.fetchScreenerResults).toHaveBeenCalledWith("cheap_value");
});

test("shows an empty-state message when results are empty", async () => {
  vi.spyOn(screenerApi, "fetchScreenerResults").mockResolvedValue({
    ...RESULTS_GAINERS, results: [],
  });
  render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/screener"]}>
      <DesktopScreenerPage />
    </MemoryRouter>,
  );
  await waitFor(() =>
    expect(screen.getByText(/표시할 종목이 없습니다/)).toBeInTheDocument(),
  );
});

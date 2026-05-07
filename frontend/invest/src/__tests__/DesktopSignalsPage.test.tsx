import { render, screen, waitFor } from "@testing-library/react";
import { vi, beforeEach, test, expect } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { DesktopSignalsPage } from "../pages/desktop/DesktopSignalsPage";
import * as signalsApi from "../api/signals";
import * as panelApi from "../api/accountPanel";

beforeEach(() => {
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue({
    homeSummary: { includedSources: [], excludedSources: [], totalValueKrw: 0 },
    accounts: [], groupedHoldings: [], watchSymbols: [], sourceVisuals: [],
    meta: { warnings: [], watchlistAvailable: true },
  });
  vi.spyOn(signalsApi, "fetchSignals").mockResolvedValue({
    tab: "mine", asOf: new Date().toISOString(),
    items: [{
      id: "analysis:1", source: "analysis", title: "삼성전자", market: "kr",
      decisionLabel: "buy", confidence: 80, generatedAt: new Date().toISOString(),
      relatedSymbols: [], relatedIssueIds: [], supportingNewsIds: [], relation: "held",
    }],
    meta: { warnings: [] },
  });
});

test("renders signal list and shows empty default detail", async () => {
  render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/signals"]}>
      <DesktopSignalsPage />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getAllByTestId("signal-list-item")).toHaveLength(1));
  expect(screen.getByText("시그널을 선택하세요.")).toBeInTheDocument();
});

test("does not render buy/sell CTA buttons", async () => {
  render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/signals"]}>
      <DesktopSignalsPage />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getAllByTestId("signal-list-item")).toHaveLength(1));
  expect(screen.queryByText(/매수/)).not.toBeInTheDocument();
  expect(screen.queryByText(/매도/)).not.toBeInTheDocument();
});

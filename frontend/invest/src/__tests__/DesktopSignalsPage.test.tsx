import { render, screen, waitFor } from "@testing-library/react";
import { vi, beforeEach, test, expect } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { DesktopSignalsPage } from "../pages/desktop/DesktopSignalsPage";
import { AccountPanelProvider } from "../desktop/AccountPanelProvider";
import * as signalsApi from "../api/signals";
import * as panelApi from "../api/accountPanel";

function wrap(ui: React.ReactElement) {
  return (
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={["/invest/signals"]}>
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
  render(wrap(<DesktopSignalsPage />));
  await waitFor(() => expect(screen.getAllByTestId("signal-list-item")).toHaveLength(1));
  expect(screen.getByText("시그널을 선택하세요.")).toBeInTheDocument();
});

test("does not render buy/sell CTA buttons", async () => {
  render(wrap(<DesktopSignalsPage />));
  await waitFor(() => expect(screen.getAllByTestId("signal-list-item")).toHaveLength(1));
  // The decision label ('매수' / '매도') renders as a decorative status pill
  // (a <span>, not actionable). The safety guarantee here is that no
  // role=button is exposed whose accessible name implies an order CTA.
  expect(screen.queryByRole("button", { name: "매수" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "매도" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /매수 주문|매도 주문/ })).not.toBeInTheDocument();
});

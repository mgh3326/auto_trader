import { vi } from "vitest";
import * as panelApi from "../api/accountPanel";
import * as signalsApi from "../api/signals";
import type { AccountPanelResponse } from "../types/invest";

const EMPTY_PANEL: AccountPanelResponse = {
  homeSummary: {
    includedSources: [],
    excludedSources: [],
    totalValueKrw: 0,
    costBasisKrw: null,
    pnlKrw: null,
    pnlRate: null,
  },
  accounts: [],
  groupedHoldings: [],
  watchSymbols: [],
  sourceVisuals: [],
  meta: { warnings: [], watchlistAvailable: true },
};

export function mockRightRail(panel: AccountPanelResponse = EMPTY_PANEL): void {
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue(panel);
  vi.spyOn(signalsApi, "fetchSignals").mockResolvedValue({
    tab: "kr",
    asOf: new Date().toISOString(),
    items: [],
    meta: { warnings: [] },
  });
}

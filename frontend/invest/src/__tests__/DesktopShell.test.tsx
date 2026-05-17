import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { DesktopShell } from "../desktop/DesktopShell";
import { AccountPanelProvider } from "../desktop/AccountPanelProvider";
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

function renderShell() {
  return render(
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={["/invest/"]}>
        <DesktopShell left={<div>L</div>} center={<div>C</div>} />
      </MemoryRouter>
    </AccountPanelProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue(EMPTY_PANEL);
  vi.spyOn(signalsApi, "fetchSignals").mockResolvedValue({
    tab: "kr",
    asOf: new Date().toISOString(),
    items: [],
    meta: { warnings: [] },
  });
  localStorage.clear();
});

test("renders left and center slots plus the always-mounted right rail panel", () => {
  renderShell();
  expect(screen.getByTestId("desktop-shell")).toBeInTheDocument();
  expect(screen.getByText("L")).toBeInTheDocument();
  expect(screen.getByText("C")).toBeInTheDocument();
  expect(screen.getByTestId("right-remote-panel")).toBeInTheDocument();
  expect(screen.getByTestId("desktop-shell")).toHaveAttribute("data-rail-collapsed", "false");
});

test("⌘. shortcut toggles the right rail collapsed state and persists it", async () => {
  const user = userEvent.setup();
  renderShell();
  expect(screen.getByTestId("desktop-shell")).toHaveAttribute("data-rail-collapsed", "false");

  await user.keyboard("{Meta>}.{/Meta}");

  expect(screen.getByTestId("desktop-shell")).toHaveAttribute("data-rail-collapsed", "true");
  expect(localStorage.getItem("invest:right-rail-collapsed")).toBe("1");
  expect(screen.getByTestId("right-remote-panel-collapsed")).toBeInTheDocument();

  await user.keyboard("{Meta>}.{/Meta}");
  expect(screen.getByTestId("desktop-shell")).toHaveAttribute("data-rail-collapsed", "false");
  expect(localStorage.getItem("invest:right-rail-collapsed")).toBe("0");
});

test("collapse button inside the pane collapses the rail", async () => {
  const user = userEvent.setup();
  renderShell();
  expect(screen.getByTestId("desktop-shell")).toHaveAttribute("data-rail-collapsed", "false");

  await user.click(screen.getByTestId("right-remote-panel-collapse"));
  expect(screen.getByTestId("desktop-shell")).toHaveAttribute("data-rail-collapsed", "true");
});

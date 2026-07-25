import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { InvestInsightsRoute } from "../pages/InsightsRoute";
import { useCommonPreferredDisparity } from "../hooks/useCommonPreferredDisparity";
import { useMarketParity } from "../hooks/useMarketParity";
import { AccountPanelProvider } from "../desktop/AccountPanelProvider";
import { mockRightRail } from "../test/mockRightRail";

// Stub the two page-owned hooks so both the desktop and mobile insights pages
// render synchronously in their loading branch — this test (an
// InvestHomeRoute.test.tsx mirror) only cares about which shell is picked,
// not the panel contents. The remaining self-fetching panels
// (ForecastCalibrationPanel/AnalysisArtifactPanel/SessionContextTimelinePanel/
// RetrospectivesPanel) are left unmocked, matching DesktopInsightsPage.test.tsx's
// existing convention of not stubbing their fetches for shell-only assertions.
vi.mock("../hooks/useMarketParity", () => ({ useMarketParity: vi.fn() }));
vi.mock("../hooks/useCommonPreferredDisparity", () => ({ useCommonPreferredDisparity: vi.fn() }));

function setWidth(w: number) {
  Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: w });
}

function wrap(ui: React.ReactElement) {
  return (
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={["/invest/insights"]}>{ui}</MemoryRouter>
    </AccountPanelProvider>
  );
}

beforeEach(() => {
  localStorage.clear();
  mockRightRail();
  vi.mocked(useMarketParity).mockReturnValue({ state: { status: "loading" }, reload: vi.fn() });
  vi.mocked(useCommonPreferredDisparity).mockReturnValue({ status: "loading" });
});

afterEach(() => vi.unstubAllGlobals());

describe("InvestInsightsRoute responsive dispatch", () => {
  it("renders the desktop shell at >= 900px", () => {
    setWidth(1280);
    render(wrap(<InvestInsightsRoute />));
    expect(screen.getByTestId("desktop-shell")).toBeInTheDocument();
    expect(screen.queryByTestId("mobile-shell")).toBeNull();
  });

  it("renders the mobile shell below 900px", () => {
    setWidth(600);
    render(wrap(<InvestInsightsRoute />));
    expect(screen.getByTestId("mobile-shell")).toBeInTheDocument();
    expect(screen.queryByTestId("desktop-shell")).toBeNull();
    // Mobile smoke: top bar + heading both carry the page title.
    expect(screen.getByTestId("mobile-top-bar")).toHaveTextContent("인사이트");
    expect(screen.getByRole("heading", { name: "인사이트" })).toBeInTheDocument();
  });
});

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { InvestHomeRoute } from "../pages/desktop/DesktopHomePage";

// Stub the data hooks so the page renders its loading branch synchronously —
// we only care about which shell is picked here.
vi.mock("../hooks/useInvestHome", () => ({
  useInvestHome: () => ({ state: { status: "loading" }, reload: () => {} }),
}));
vi.mock("../hooks/useMarketDashboard", () => ({
  useMarketDashboard: () => ({ state: { status: "loading" }, reload: () => {} }),
}));
vi.mock("../hooks/useMarketParity", () => ({
  useMarketParity: () => ({ state: { status: "loading" }, reload: () => {} }),
}));
vi.mock("../desktop/useAccountPanel", () => ({
  useAccountPanel: () => ({ data: undefined, error: undefined, loading: true, reload: () => {} }),
}));

function setWidth(w: number) {
  Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: w });
}

describe("InvestHomeRoute responsive dispatch", () => {
  it("renders the desktop shell at >= 900px", () => {
    setWidth(1280);
    render(
      <MemoryRouter basename="/invest" initialEntries={["/invest/"]}>
        <InvestHomeRoute />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("desktop-shell")).toBeInTheDocument();
    expect(screen.queryByTestId("mobile-shell")).toBeNull();
  });

  it("renders the mobile shell below 900px", () => {
    setWidth(600);
    render(
      <MemoryRouter basename="/invest" initialEntries={["/invest/"]}>
        <InvestHomeRoute />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("mobile-shell")).toBeInTheDocument();
    expect(screen.queryByTestId("desktop-shell")).toBeNull();
  });
});

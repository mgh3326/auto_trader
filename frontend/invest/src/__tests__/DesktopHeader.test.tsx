import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { DesktopHeader } from "../desktop/DesktopHeader";

function renderAt(path: string) {
  const router = createMemoryRouter(
    [{ path: "*", element: <DesktopHeader /> }],
    { initialEntries: [`/invest${path}`], basename: "/invest" },
  );
  return render(<RouterProvider router={router} />);
}

describe("DesktopHeader", () => {
  it("exposes an 인사이트 nav link to /invest/insights", () => {
    renderAt("/");
    expect(screen.getByRole("link", { name: /인사이트/ })).toHaveAttribute(
      "href",
      "/invest/insights",
    );
  });

  it("places 인사이트 between 커버리지 and 골라보기 in the nav order", () => {
    renderAt("/");
    const labels = screen.getAllByRole("link").map((el) => el.textContent ?? "");
    const coverage = labels.findIndex((t) => t.includes("커버리지"));
    const insights = labels.findIndex((t) => t.includes("인사이트"));
    const screener = labels.findIndex((t) => t.includes("골라보기"));
    expect(coverage).toBeGreaterThanOrEqual(0);
    expect(insights).toBe(coverage + 1);
    expect(screener).toBe(insights + 1);
  });
});

import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { MobileBottomNav } from "../mobile/MobileBottomNav";

function renderAt(path: string) {
  const router = createMemoryRouter(
    [{ path: "*", element: <MobileBottomNav /> }],
    { initialEntries: [`/invest${path}`], basename: "/invest" },
  );
  return render(<RouterProvider router={router} />);
}

describe("MobileBottomNav", () => {
  it("renders all five canonical tabs", () => {
    renderAt("/");
    expect(screen.getByRole("link", { name: /홈/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /뉴스/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /발견/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /시그널/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /캘린더/ })).toBeInTheDocument();
  });

  it("뉴스 link points to /invest/feed/news", () => {
    renderAt("/");
    expect(screen.getByRole("link", { name: /뉴스/ })).toHaveAttribute("href", "/invest/feed/news");
  });

  it("highlights active tab via --fg color", () => {
    renderAt("/feed/news");
    const link = screen.getByRole("link", { name: /뉴스/ });
    expect(link.getAttribute("style") ?? "").toContain("color: var(--fg)");
  });
});

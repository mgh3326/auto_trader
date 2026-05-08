// frontend/invest/src/__tests__/BottomNav.test.tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { expect, test, vi } from "vitest";
import { BottomNav } from "../components/BottomNav";

function renderAt(path: string) {
  const router = createMemoryRouter(
    [{ path: "*", element: <BottomNav /> }],
    { initialEntries: [`/invest/app${path}`], basename: "/invest" },
  );
  return render(<RouterProvider router={router} />);
}

test("발견 link points to /invest/app/discover", () => {
  renderAt("/");
  const link = screen.getByRole("link", { name: "발견" });
  expect(link).toHaveAttribute("href", "/invest/app/discover");
});

test("증권 link points to /invest/app", () => {
  renderAt("/");
  const link = screen.getByRole("link", { name: "증권" });
  expect(link).toHaveAttribute("href", "/invest/app");
});

test("관심 and 피드 are aria-disabled and do not call alert when clicked", async () => {
  const user = userEvent.setup();
  const alertSpy = vi.spyOn(globalThis, "alert").mockImplementation(() => {});
  renderAt("/");

  const watch = screen.getByRole("button", { name: "관심" });
  const feed = screen.getByRole("button", { name: "피드" });
  expect(watch).toHaveAttribute("aria-disabled", "true");
  expect(feed).toHaveAttribute("aria-disabled", "true");

  await user.click(watch);
  await user.click(feed);
  expect(alertSpy).not.toHaveBeenCalled();
  alertSpy.mockRestore();
});

test("BottomNav highlights active tab", () => {
  renderAt("/discover");
  const link = screen.getByRole("link", { name: "발견" });
  // active uses var(--text), inactive uses var(--muted)
  expect(link.getAttribute("style") ?? "").toContain("color: var(--text)");
});

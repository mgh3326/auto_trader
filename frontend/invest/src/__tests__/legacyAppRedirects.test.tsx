import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RouterProvider, createMemoryRouter, Navigate, useParams } from "react-router-dom";

// Stub the canonical pages with sentinel components so we can assert
// which route element rendered after a redirect, without pulling in
// the data-fetching machinery of the real pages.
vi.mock("../pages/desktop/DesktopHomePage", () => ({
  InvestHomeRoute: () => <div data-testid="canonical-home" />,
}));
vi.mock("../pages/desktop/DesktopFeedNewsPage", () => ({
  FeedNewsRoute: () => <div data-testid="canonical-news" />,
}));
vi.mock("../pages/desktop/DesktopDiscoverPage", () => ({
  InvestDiscoverRoute: () => <div data-testid="canonical-discover" />,
}));
vi.mock("../pages/desktop/DesktopSignalsPage", () => ({
  SignalsRoute: () => <div data-testid="canonical-signals" />,
}));
vi.mock("../pages/desktop/DesktopCalendarPage", () => ({
  CalendarRoute: () => <div data-testid="canonical-calendar" />,
}));
vi.mock("../pages/desktop/DesktopScreenerPage", () => ({
  DesktopScreenerPage: () => <div data-testid="canonical-screener" />,
}));
vi.mock("../pages/DiscoverIssueDetailPage", () => ({
  DiscoverIssueDetailPage: () => {
    const { issueId } = useParams();
    return <div data-testid="canonical-issue-detail" data-issue-id={issueId} />;
  },
}));

afterEach(() => {
  vi.resetModules();
});

async function renderRoute(initialPath: string) {
  // Re-import the router fresh per case so each test starts from the
  // initialEntries we provide rather than carrying state across tests.
  const { router: realRouter } = await import("../routes");
  // The real router is a browser router; for jsdom we recreate the
  // same route table on a memory router with the user's entry.
  const routes = realRouter.routes;
  const memory = createMemoryRouter(routes, {
    basename: "/invest",
    initialEntries: [`/invest${initialPath}`],
  });
  return render(<RouterProvider router={memory} />);
}

describe("legacy /invest/app/* redirects", () => {
  it("/app -> /", async () => {
    await renderRoute("/app");
    await waitFor(() => expect(screen.getByTestId("canonical-home")).toBeInTheDocument());
  });

  it("/app/discover -> /discover", async () => {
    await renderRoute("/app/discover");
    await waitFor(() => expect(screen.getByTestId("canonical-discover")).toBeInTheDocument());
  });

  it("/app/discover/issues/:id -> /discover/issues/:id (preserving the id param)", async () => {
    await renderRoute("/app/discover/issues/iss-xyz");
    const detail = await waitFor(() => screen.getByTestId("canonical-issue-detail"));
    expect(detail.getAttribute("data-issue-id")).toBe("iss-xyz");
  });

  it("/app/paper -> /", async () => {
    await renderRoute("/app/paper");
    await waitFor(() => expect(screen.getByTestId("canonical-home")).toBeInTheDocument());
  });

  it("/app/paper/:variant -> /", async () => {
    await renderRoute("/app/paper/cycle");
    await waitFor(() => expect(screen.getByTestId("canonical-home")).toBeInTheDocument());
  });

  it("unknown /app path falls through to the catch-all -> /", async () => {
    await renderRoute("/app/something-unknown");
    await waitFor(() => expect(screen.getByTestId("canonical-home")).toBeInTheDocument());
  });
});

describe("canonical routes still resolve", () => {
  it("/ renders the canonical home", async () => {
    await renderRoute("/");
    await waitFor(() => expect(screen.getByTestId("canonical-home")).toBeInTheDocument());
  });

  it("/discover renders the canonical discover", async () => {
    await renderRoute("/discover");
    await waitFor(() => expect(screen.getByTestId("canonical-discover")).toBeInTheDocument());
  });

  it("/calendar renders the canonical calendar", async () => {
    await renderRoute("/calendar");
    await waitFor(() => expect(screen.getByTestId("canonical-calendar")).toBeInTheDocument());
  });
});

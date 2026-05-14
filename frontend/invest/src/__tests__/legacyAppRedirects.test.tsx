import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RouterProvider, createMemoryRouter, useLocation, useParams } from "react-router-dom";

// Stub the canonical pages with sentinel components that surface the
// router's path / search / hash via data-attributes. Tests can then
// assert the sentinel rendered (correct redirect target) AND that
// search/hash from the legacy URL survived the redirect.
function makeSentinel(testId: string) {
  return function Sentinel() {
    const { pathname, search, hash } = useLocation();
    return (
      <div
        data-testid={testId}
        data-pathname={pathname}
        data-search={search}
        data-hash={hash}
      />
    );
  };
}

vi.mock("../pages/desktop/DesktopHomePage", () => ({
  InvestHomeRoute: makeSentinel("canonical-home"),
}));
vi.mock("../pages/desktop/DesktopFeedNewsPage", () => ({
  FeedNewsRoute: makeSentinel("canonical-news"),
}));
vi.mock("../pages/desktop/DesktopDiscoverPage", () => ({
  InvestDiscoverRoute: makeSentinel("canonical-discover"),
}));
vi.mock("../pages/desktop/DesktopCalendarPage", () => ({
  CalendarRoute: makeSentinel("canonical-calendar"),
}));
vi.mock("../pages/desktop/DesktopScreenerPage", () => ({
  DesktopScreenerPage: makeSentinel("canonical-screener"),
}));
vi.mock("../pages/DiscoverIssueDetailPage", () => ({
  DiscoverIssueDetailPage: () => {
    const { issueId } = useParams();
    const { search, hash } = useLocation();
    return (
      <div
        data-testid="canonical-issue-detail"
        data-issue-id={issueId}
        data-search={search}
        data-hash={hash}
      />
    );
  },
}));

afterEach(() => {
  vi.resetModules();
});

async function renderRoute(initialPath: string) {
  const { router: realRouter } = await import("../routes");
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

describe("legacy /invest/app/* redirects preserve query and hash", () => {
  it("/app?market=kr -> / preserves ?market=kr", async () => {
    await renderRoute("/app?market=kr");
    const home = await waitFor(() => screen.getByTestId("canonical-home"));
    expect(home.getAttribute("data-search")).toBe("?market=kr");
  });

  it("/app/discover?market=kr&window=24 -> /discover preserves the full search string", async () => {
    await renderRoute("/app/discover?market=kr&window=24");
    const discover = await waitFor(() => screen.getByTestId("canonical-discover"));
    expect(discover.getAttribute("data-search")).toBe("?market=kr&window=24");
  });

  it("/app/discover/issues/:id?market=kr#article-3 -> canonical detail preserves both search and hash", async () => {
    await renderRoute("/app/discover/issues/abc?market=kr#article-3");
    const detail = await waitFor(() => screen.getByTestId("canonical-issue-detail"));
    expect(detail.getAttribute("data-issue-id")).toBe("abc");
    expect(detail.getAttribute("data-search")).toBe("?market=kr");
    expect(detail.getAttribute("data-hash")).toBe("#article-3");
  });

  it("/app/paper?variant=cycle -> / preserves the search", async () => {
    await renderRoute("/app/paper?variant=cycle");
    const home = await waitFor(() => screen.getByTestId("canonical-home"));
    expect(home.getAttribute("data-search")).toBe("?variant=cycle");
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

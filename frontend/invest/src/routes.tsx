// frontend/invest/src/routes.tsx
import { createBrowserRouter, Navigate, useLocation, useParams } from "react-router-dom";
import { DiscoverIssueDetailPage } from "./pages/DiscoverIssueDetailPage";
import { InvestHomeRoute } from "./pages/desktop/DesktopHomePage";
import { FeedNewsRoute } from "./pages/desktop/DesktopFeedNewsPage";
import { InvestDiscoverRoute } from "./pages/desktop/DesktopDiscoverPage";
import { SignalsRoute } from "./pages/desktop/DesktopSignalsPage";
import { CalendarRoute } from "./pages/desktop/DesktopCalendarPage";
import { DesktopScreenerPage } from "./pages/desktop/DesktopScreenerPage";

// Static legacy /app/* redirect that preserves any ?search and #hash
// from the source URL so market-scoped or anchor-scoped bookmarks
// (e.g. /invest/app/discover?market=kr) keep their context after the
// hop to canonical.
function RedirectWithSearch({ to }: { to: string }) {
  const { search, hash } = useLocation();
  return <Navigate to={`${to}${search}${hash}`} replace />;
}

// Same idea for the dynamic /app/discover/issues/:issueId case —
// preserve the param plus search/hash on the canonical path.
function DiscoverIssueRedirect() {
  const { issueId } = useParams();
  const { search, hash } = useLocation();
  return <Navigate to={`/discover/issues/${issueId ?? ""}${search}${hash}`} replace />;
}

export const router = createBrowserRouter(
  [
    // Canonical /invest routes — the home view is responsive
    // (DesktopHomePage at >=900px, MobileHomePage below).
    { path: "/", element: <InvestHomeRoute /> },
    { path: "/feed/news", element: <FeedNewsRoute /> },
    { path: "/discover", element: <InvestDiscoverRoute /> },
    { path: "/discover/issues/:issueId", element: <DiscoverIssueDetailPage /> },
    { path: "/signals", element: <SignalsRoute /> },
    { path: "/calendar", element: <CalendarRoute /> },
    { path: "/screener", element: <DesktopScreenerPage /> },

    // Stage 6: legacy /invest/app/* URLs redirect to their canonical
    // /invest/* siblings. The legacy components remain in-tree (not
    // mounted) for one release cycle; deletion lands in a follow-up
    // PR per docs/plans/2026-05-09-invest-app-retirement-inventory.md.
    // Each redirect preserves the original ?search and #hash.
    { path: "/app", element: <RedirectWithSearch to="/" /> },
    { path: "/app/paper", element: <RedirectWithSearch to="/" /> },
    { path: "/app/paper/:variant", element: <RedirectWithSearch to="/" /> },
    { path: "/app/discover", element: <RedirectWithSearch to="/discover" /> },
    { path: "/app/discover/issues/:issueId", element: <DiscoverIssueRedirect /> },

    { path: "*", element: <Navigate to="/" replace /> },
  ],
  { basename: "/invest" },
);

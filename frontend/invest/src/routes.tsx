// frontend/invest/src/routes.tsx
import { createBrowserRouter, Navigate, useParams } from "react-router-dom";
import { DiscoverIssueDetailPage } from "./pages/DiscoverIssueDetailPage";
import { InvestHomeRoute } from "./pages/desktop/DesktopHomePage";
import { FeedNewsRoute } from "./pages/desktop/DesktopFeedNewsPage";
import { InvestDiscoverRoute } from "./pages/desktop/DesktopDiscoverPage";
import { SignalsRoute } from "./pages/desktop/DesktopSignalsPage";
import { CalendarRoute } from "./pages/desktop/DesktopCalendarPage";
import { DesktopScreenerPage } from "./pages/desktop/DesktopScreenerPage";

// Stage 6: redirect dynamic legacy /app/discover/issues/:issueId path
// to the canonical /discover/issues/:issueId, preserving the issueId
// param so external bookmarks survive the rename.
function DiscoverIssueRedirect() {
  const { issueId } = useParams();
  return <Navigate to={`/discover/issues/${issueId ?? ""}`} replace />;
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
    { path: "/app", element: <Navigate to="/" replace /> },
    { path: "/app/paper", element: <Navigate to="/" replace /> },
    { path: "/app/paper/:variant", element: <Navigate to="/" replace /> },
    { path: "/app/discover", element: <Navigate to="/discover" replace /> },
    { path: "/app/discover/issues/:issueId", element: <DiscoverIssueRedirect /> },

    { path: "*", element: <Navigate to="/" replace /> },
  ],
  { basename: "/invest" },
);

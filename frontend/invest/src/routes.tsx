// frontend/invest/src/routes.tsx
import { createBrowserRouter, Navigate } from "react-router-dom";
import { DiscoverIssueDetailPage } from "./pages/DiscoverIssueDetailPage";
import { DiscoverPage } from "./pages/DiscoverPage";
import { HomePage } from "./pages/HomePage";
import { PaperPlaceholderPage } from "./pages/PaperPlaceholderPage";
import { InvestHomeRoute } from "./pages/desktop/DesktopHomePage";
import { DesktopFeedNewsPage } from "./pages/desktop/DesktopFeedNewsPage";
import { DesktopSignalsPage } from "./pages/desktop/DesktopSignalsPage";
import { DesktopCalendarPage } from "./pages/desktop/DesktopCalendarPage";
import { DesktopScreenerPage } from "./pages/desktop/DesktopScreenerPage";

export const router = createBrowserRouter(
  [
    // Canonical /invest routes — the home view is responsive
    // (DesktopHomePage at >=900px, MobileHomePage below).
    { path: "/", element: <InvestHomeRoute /> },
    { path: "/feed/news", element: <DesktopFeedNewsPage /> },
    { path: "/signals", element: <DesktopSignalsPage /> },
    { path: "/calendar", element: <DesktopCalendarPage /> },
    { path: "/screener", element: <DesktopScreenerPage /> },

    // Legacy /invest/app/* surface — preserved until Stage 6 retires it.
    { path: "/app", element: <HomePage /> },
    { path: "/app/paper", element: <PaperPlaceholderPage /> },
    { path: "/app/paper/:variant", element: <PaperPlaceholderPage /> },
    { path: "/app/discover", element: <DiscoverPage /> },
    { path: "/app/discover/issues/:issueId", element: <DiscoverIssueDetailPage /> },

    { path: "*", element: <Navigate to="/" replace /> },
  ],
  { basename: "/invest" },
);

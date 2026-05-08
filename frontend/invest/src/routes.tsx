// frontend/invest/src/routes.tsx
import { createBrowserRouter, Navigate } from "react-router-dom";
import { DiscoverIssueDetailPage } from "./pages/DiscoverIssueDetailPage";
import { DiscoverPage } from "./pages/DiscoverPage";
import { HomePage } from "./pages/HomePage";
import { PaperPlaceholderPage } from "./pages/PaperPlaceholderPage";
import { DesktopHomePage } from "./pages/desktop/DesktopHomePage";
import { DesktopFeedNewsPage } from "./pages/desktop/DesktopFeedNewsPage";
import { DesktopSignalsPage } from "./pages/desktop/DesktopSignalsPage";
import { DesktopCalendarPage } from "./pages/desktop/DesktopCalendarPage";

export const router = createBrowserRouter(
  [
    { path: "/", element: <DesktopHomePage /> },
    { path: "/feed/news", element: <DesktopFeedNewsPage /> },
    { path: "/signals", element: <DesktopSignalsPage /> },
    { path: "/calendar", element: <DesktopCalendarPage /> },

    { path: "/app", element: <HomePage /> },
    { path: "/app/paper", element: <PaperPlaceholderPage /> },
    { path: "/app/paper/:variant", element: <PaperPlaceholderPage /> },
    { path: "/app/discover", element: <DiscoverPage /> },
    { path: "/app/discover/issues/:issueId", element: <DiscoverIssueDetailPage /> },

    { path: "*", element: <Navigate to="/" replace /> },
  ],
  { basename: "/invest" },
);

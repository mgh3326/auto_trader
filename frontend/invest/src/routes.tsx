// frontend/invest/src/routes.tsx
import { createBrowserRouter, Navigate } from "react-router-dom";
import { DiscoverIssueDetailPage } from "./pages/DiscoverIssueDetailPage";
import { DiscoverPage } from "./pages/DiscoverPage";
import { HomePage } from "./pages/HomePage";
import { PaperPlaceholderPage } from "./pages/PaperPlaceholderPage";

export const router = createBrowserRouter(
  [
    // Mobile (existing) under /app/*
    { path: "/app", element: <HomePage /> },
    { path: "/app/paper", element: <PaperPlaceholderPage /> },
    { path: "/app/paper/:variant", element: <PaperPlaceholderPage /> },
    { path: "/app/discover", element: <DiscoverPage /> },
    { path: "/app/discover/issues/:issueId", element: <DiscoverIssueDetailPage /> },

    // Desktop (added in Tasks 8-11)
    // { path: "/", element: <DesktopHomePage /> },
    // { path: "/feed/news", element: <DesktopFeedNewsPage /> },
    // { path: "/signals", element: <DesktopSignalsPage /> },
    // { path: "/calendar", element: <DesktopCalendarPage /> },

    { path: "*", element: <Navigate to="/app" replace /> },
  ],
  { basename: "/invest" },
);

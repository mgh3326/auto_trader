// frontend/invest/src/routes.tsx
import { createBrowserRouter, Navigate } from "react-router-dom";
import { DiscoverIssueDetailPage } from "./pages/DiscoverIssueDetailPage";
import { DiscoverPage } from "./pages/DiscoverPage";
import { HomePage } from "./pages/HomePage";
import { PaperPlaceholderPage } from "./pages/PaperPlaceholderPage";

export const router = createBrowserRouter(
  [
    { path: "/", element: <HomePage /> },
    { path: "/paper", element: <PaperPlaceholderPage /> },
    { path: "/paper/:variant", element: <PaperPlaceholderPage /> },
    { path: "/discover", element: <DiscoverPage /> },
    { path: "/discover/issues/:issueId", element: <DiscoverIssueDetailPage /> },
    { path: "*", element: <Navigate to="/" replace /> },
  ],
  { basename: "/invest/app" },
);

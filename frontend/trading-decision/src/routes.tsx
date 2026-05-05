import { createBrowserRouter, type RouteObject, useParams } from "react-router-dom";
import PreopenPage from "./pages/PreopenPage";
import NewsRadarPage from "./pages/NewsRadarPage";
import SessionDetailPage from "./pages/SessionDetailPage";
import SessionListPage from "./pages/SessionListPage";
import ResearchHomePage from "./pages/ResearchHomePage";
import ResearchSessionDetailPage from "./pages/ResearchSessionDetailPage";
import SymbolTimelinePage from "./pages/SymbolTimelinePage";

const SESSION_UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function isTradingDecisionSessionUuid(value: string | undefined): value is string {
  return Boolean(value && SESSION_UUID_RE.test(value));
}

function LegacySessionDetailAlias() {
  const { sessionUuid } = useParams();

  if (!isTradingDecisionSessionUuid(sessionUuid)) {
    return <SessionListPage />;
  }

  return <SessionDetailPage />;
}

export const tradingDecisionRoutes: RouteObject[] = [
  { path: "/", element: <SessionListPage /> },
  { path: "/preopen", element: <PreopenPage /> },
  { path: "/news-radar", element: <NewsRadarPage /> },
  { path: "/sessions/:sessionUuid", element: <SessionDetailPage /> },
  { path: "/research", element: <ResearchHomePage /> },
  { path: "/research/sessions/:sessionId", element: <ResearchSessionDetailPage /> },
  { path: "/research/symbols/:symbol/timeline", element: <SymbolTimelinePage /> },
  // Backward-compatible alias for UUID session URLs generated before the
  // canonical /sessions/:sessionUuid route was adopted. Keep arbitrary
  // single-segment paths on the list page instead of treating them as sessions.
  { path: "/:sessionUuid", element: <LegacySessionDetailAlias /> },
  { path: "*", element: <SessionListPage /> },
];

export const router = createBrowserRouter(tradingDecisionRoutes, {
  basename: "/trading/decisions",
});

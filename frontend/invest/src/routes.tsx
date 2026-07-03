// frontend/invest/src/routes.tsx
//
// /invest route contract (ROB-195)
// ─────────────────────────────────────────────────────────────────────────────
// /invest (/)               — Home / market-entry role.
//                             Shows: account summary hero, market index strip,
//                             key navigation shortcuts.
//                             Does NOT show a full holdings ledger.
//
// /invest/my                — Detailed holdings / portfolio table.
//                             Shows: hero summary + full holdings table with
//                             account/category filtering.
//                             Naver-style unified table implemented by ROB-196.
//
// /invest/feed/news         — News and research feed.
// /invest/discover          — Issue discovery / investment ideas.
// /invest/my?tab=signals    — AI analysis signals within MY.
// /invest/calendar          — Earnings/events calendar.
// /invest/coverage          — Data coverage dashboard.
// /invest/insights          — Read-only market insight cards (responsive).
// /invest/screener          — Stock screener (골라보기).
// /invest/reports                 — Investment report list (ROB-265).
// /invest/reports/:reportUuid     — Single investment report bundle.
// /invest/stocks/:m/:sym    — Stock detail page.
// ─────────────────────────────────────────────────────────────────────────────
import { createBrowserRouter, Navigate, useLocation, useParams } from "react-router-dom";
import { DiscoverIssueDetailPage } from "./pages/DiscoverIssueDetailPage";
import { InvestHomeRoute } from "./pages/desktop/DesktopHomePage";
import { InvestPortfolioRoute } from "./pages/desktop/DesktopPortfolioPage";
import { FeedNewsRoute } from "./pages/desktop/DesktopFeedNewsPage";
import { InvestDiscoverRoute } from "./pages/desktop/DesktopDiscoverPage";
import { CalendarRoute } from "./pages/desktop/DesktopCalendarPage";
import { CoverageRoute } from "./pages/desktop/DesktopCoveragePage";
import { DesktopScreenerPage } from "./pages/desktop/DesktopScreenerPage";
import { DesktopMarketPage } from "./pages/desktop/DesktopMarketPage";
import { InvestInsightsRoute } from "./pages/InsightsRoute";
import { FxMacroRoute } from "./pages/desktop/FxMacroPage";
import { DesktopCryptoPage } from "./pages/desktop/DesktopCryptoPage";
import { ScalpingRoute } from "./pages/desktop/DesktopScalpingPage";
import {
  InvestmentReportBundleRoute,
  InvestmentReportsRoute,
} from "./pages/desktop/DesktopInvestmentReportsPage";
import { StockDetailPage } from "./pages/stock-detail/StockDetailPage";

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

function CryptoPairRedirect() {
  const { pair } = useParams();
  const { search, hash } = useLocation();
  return <Navigate to={`/stocks/crypto/${encodeURIComponent(pair ?? "")}${search}${hash}`} replace />;
}

export const router = createBrowserRouter(
  [
    // Canonical /invest routes — the home view is responsive
    // (DesktopHomePage at >=900px, MobileHomePage below).
    // Home role: market entry / account summary / navigation shortcuts only.
    { path: "/", element: <InvestHomeRoute /> },

    // Portfolio/holdings — dedicated surface for the full holdings ledger.
    // Home (/invest) intentionally omits this to avoid being a holdings duplicate.
    { path: "/my", element: <InvestPortfolioRoute /> },

    { path: "/feed/news", element: <FeedNewsRoute /> },
    { path: "/discover", element: <InvestDiscoverRoute /> },
    { path: "/discover/issues/:issueId", element: <DiscoverIssueDetailPage /> },
    { path: "/calendar", element: <CalendarRoute /> },
    { path: "/coverage", element: <CoverageRoute /> },
    { path: "/market", element: <DesktopMarketPage /> },
    { path: "/insights", element: <InvestInsightsRoute /> },
    { path: "/market/fx", element: <FxMacroRoute /> },
    { path: "/crypto", element: <DesktopCryptoPage /> },
    { path: "/crypto/:pair", element: <CryptoPairRedirect /> },
    { path: "/screener", element: <DesktopScreenerPage /> },
    { path: "/scalping", element: <ScalpingRoute /> },
    { path: "/reports", element: <InvestmentReportsRoute /> },
    { path: "/reports/:reportUuid", element: <InvestmentReportBundleRoute /> },
    { path: "/stocks/:market/:symbol", element: <StockDetailPage /> },

    // Legacy /invest/app/* URLs redirect to their canonical /invest/*
    // siblings. The retired legacy components were removed after the
    // one-release-cycle soak per
    // docs/plans/2026-05-09-invest-app-retirement-inventory.md.
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

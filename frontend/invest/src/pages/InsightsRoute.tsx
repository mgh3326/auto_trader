// /invest/insights — read-only market insight cards.
// Single canonical route wrapper — picks the desktop or mobile renderer based
// on viewport width, mirroring InvestHomeRoute (pages/desktop/DesktopHomePage.tsx).
//
// Kept in its own file (rather than alongside DesktopInsightsPage.tsx, the
// usual sibling-surface convention) because ROB-682 edits
// pages/desktop/DesktopInsightsPage.tsx in parallel; keeping the wrapper here
// avoids a merge conflict on that file (ROB-681).
import { useViewport } from "../hooks/useViewport";
import { DesktopInsightsPage } from "./desktop/DesktopInsightsPage";
import { MobileInsightsPage } from "./mobile/MobileInsightsPage";

export function InvestInsightsRoute() {
  const viewport = useViewport();
  return viewport === "mobile" ? <MobileInsightsPage /> : <DesktopInsightsPage />;
}

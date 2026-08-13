// /invest/watches — INVEST-WATCH-UI §57차 item ①. Mobile-first viewport
// dispatch, mirrors InsightsRoute.tsx / InvestHomeRoute.
import { useViewport } from "../hooks/useViewport";
import { DesktopWatchesPage } from "./desktop/DesktopWatchesPage";
import { MobileWatchesPage } from "./mobile/MobileWatchesPage";

export function WatchesRoute() {
  const viewport = useViewport();
  return viewport === "mobile" ? <MobileWatchesPage /> : <DesktopWatchesPage />;
}

// /invest/orders/:broker/:ledgerId — INVEST-WATCH-UI §57차 item ②. Mobile-first
// viewport dispatch, mirrors WatchesRoute.tsx / InsightsRoute.tsx.
import { useViewport } from "../hooks/useViewport";
import { DesktopOrderDetailPage } from "./desktop/DesktopOrderDetailPage";
import { MobileOrderDetailPage } from "./mobile/MobileOrderDetailPage";

export function OrderDetailRoute() {
  const viewport = useViewport();
  return viewport === "mobile" ? <MobileOrderDetailPage /> : <DesktopOrderDetailPage />;
}

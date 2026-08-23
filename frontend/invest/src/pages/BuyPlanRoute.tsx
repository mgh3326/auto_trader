// /invest/buy-plan — §144차. Viewport dispatch, mirrors WatchesRoute.tsx.
import { useViewport } from "../hooks/useViewport";
import { DesktopBuyPlanPage } from "./desktop/DesktopBuyPlanPage";
import { MobileBuyPlanPage } from "./mobile/MobileBuyPlanPage";

export function BuyPlanRoute() {
  const viewport = useViewport();
  return viewport === "mobile" ? <MobileBuyPlanPage /> : <DesktopBuyPlanPage />;
}

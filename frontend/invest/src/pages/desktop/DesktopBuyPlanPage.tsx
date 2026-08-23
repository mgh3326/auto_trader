// /invest/buy-plan (desktop) — §144차.
import { BuyPlanPageBody } from "../../components/buyPlan/BuyPlanPageBody";
import { DesktopShell } from "../../desktop/DesktopShell";

export function DesktopBuyPlanPage() {
  return <DesktopShell center={<BuyPlanPageBody />} />;
}

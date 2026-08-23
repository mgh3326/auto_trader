// /invest/buy-plan (mobile) — §144차.
import { BuyPlanPageBody } from "../../components/buyPlan/BuyPlanPageBody";
import { MobileShell } from "../../mobile/MobileShell";

export function MobileBuyPlanPage() {
  return (
    <MobileShell title="매수 계획">
      <div style={{ padding: "14px 16px 24px" }}>
        <BuyPlanPageBody />
      </div>
    </MobileShell>
  );
}

// /invest/orders/:broker/:ledgerId (mobile) — INVEST-WATCH-UI §57차 item ②.
import { OrderDetailBody } from "../../components/orders/OrderDetailBody";
import { MobileShell } from "../../mobile/MobileShell";

export function MobileOrderDetailPage() {
  return (
    <MobileShell title="주문 상세">
      <div style={{ padding: "14px 16px 24px" }}>
        <OrderDetailBody />
      </div>
    </MobileShell>
  );
}

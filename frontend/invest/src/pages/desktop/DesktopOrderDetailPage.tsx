// /invest/orders/:broker/:ledgerId (desktop) — INVEST-WATCH-UI §57차 item ②.
import { OrderDetailBody } from "../../components/orders/OrderDetailBody";
import { DesktopShell } from "../../desktop/DesktopShell";

export function DesktopOrderDetailPage() {
  return <DesktopShell center={<OrderDetailBody />} />;
}

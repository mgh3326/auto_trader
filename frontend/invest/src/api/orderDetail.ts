// INVEST-WATCH-UI §57차 item ② — single order-ledger row (ledger + linked
// report-item thesis + lifecycle) for the standalone /invest/orders detail
// page. Backend: GET /trading/api/invest/fills/order-detail (ROB-554
// projection reused server-side so this can never drift from the
// stock-detail order-ledger card / report-bundle decision log).
import type { LinkedOrder } from "../types/investmentReports";
import { normalizeLinkedOrder } from "./investmentReports";

const BASE = "/trading/api/invest/fills/order-detail";

export class OrderDetailNotFoundError extends Error {}

export async function fetchOrderDetail(broker: string, ledgerId: number): Promise<LinkedOrder> {
  const q = new URLSearchParams({ broker, ledger_id: String(ledgerId) });
  const res = await fetch(`${BASE}?${q}`, { credentials: "include" });
  if (res.status === 404) throw new OrderDetailNotFoundError("order not found");
  if (!res.ok) throw new Error(`order-detail ${res.status}`);
  return normalizeLinkedOrder(await res.json());
}

// INVEST-WATCH-UI §57차 item ② — single order-ledger row (ledger + linked
// report-item thesis + lifecycle) for the standalone /invest/orders detail
// page. Backend: GET /trading/api/invest/fills/order-detail (ROB-554
// projection reused server-side so this can never drift from the
// stock-detail order-ledger card / report-bundle decision log).
import type { LinkedOrder } from "../types/investmentReports";
import { normalizeLinkedOrder } from "./investmentReports";

const BASE = "/trading/api/invest/fills/order-detail";

export class OrderDetailNotFoundError extends Error {}
export class OrderDetailUnknownLedgerError extends Error {}

// `market` is required alongside `broker` — verify-r1 BLOCKER-1: the literal
// broker value "kis" is written to two different ledger tables (KR domestic
// vs. US live via KIS), each with an independent id sequence, so
// broker+ledgerId alone can resolve to an unrelated order. Must match the
// backend's (broker, market) -> table allowlist in
// app/routers/invest_fills.py::order_detail.
export async function fetchOrderDetail(
  broker: string,
  market: string,
  ledgerId: number,
): Promise<LinkedOrder> {
  const q = new URLSearchParams({ broker, market, ledger_id: String(ledgerId) });
  const res = await fetch(`${BASE}?${q}`, { credentials: "include" });
  if (res.status === 404) throw new OrderDetailNotFoundError("order not found");
  if (res.status === 400) throw new OrderDetailUnknownLedgerError("unknown ledger combination");
  if (!res.ok) throw new Error(`order-detail ${res.status}`);
  return normalizeLinkedOrder(await res.json());
}

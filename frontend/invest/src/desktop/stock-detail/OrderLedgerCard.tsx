// ROB-559 — per-symbol live order history card for the stock detail page.
// Renders the same LinkedOrderRow as the report-bundle decision log so a symbol's
// orders show status + rationale + fill rollup consistently.

import {
  LinkedOrderRow,
  linkedOrderKey,
} from "../../components/orders/LinkedOrderRow";
import { Card } from "../../ds";
import type { LinkedOrder } from "../../types/investmentReports";

export function OrderLedgerCard({
  orders,
}: {
  orders: LinkedOrder[] | undefined;
}) {
  return (
    <Card data-testid="stock-detail-order-ledger">
      <h2 style={{ margin: "0 0 8px", fontSize: 16 }}>주문 기록</h2>
      {!orders ? (
        <p style={{ margin: 0, color: "var(--fg-3)" }}>불러오는 중입니다…</p>
      ) : null}
      {orders && orders.length === 0 ? (
        <p style={{ margin: 0, color: "var(--fg-3)" }}>주문 기록이 없습니다.</p>
      ) : null}
      {orders && orders.length > 0 ? (
        <div style={{ display: "grid", gap: 6 }}>
          {orders.map((order) => (
            <LinkedOrderRow key={linkedOrderKey(order)} order={order} />
          ))}
        </div>
      ) : null}
    </Card>
  );
}

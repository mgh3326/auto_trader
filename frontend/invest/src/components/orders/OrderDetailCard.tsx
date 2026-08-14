// Standalone order/fill detail body (INVEST-WATCH-UI §57차 item ②) — ledger
// row (symbol · qty · price · lifecycle) combined with the send-time thesis.
// Reuses the same status vocabulary as LinkedOrderRow (the per-symbol list
// row on the stock-detail page) so the two views never disagree on labels.
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Card, Pill } from "../../ds";
import { stockDetailPath } from "../../stockDetailPath";
import type { LinkedOrder } from "../../types/investmentReports";
import {
  LINKED_ORDER_STATUS_LABELS,
  LINKED_ORDER_STATUS_TONES,
} from "./LinkedOrderRow";

function fmtAmount(value: number | string | null | undefined): string {
  if (value == null || value === "") return "—";
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: 8 }) : String(value);
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={{ display: "grid", gap: 2 }}>
      <div style={{ fontSize: 11, color: "var(--fg-3)" }}>{label}</div>
      <div style={{ fontSize: 14, fontWeight: 600 }}>{children}</div>
    </div>
  );
}

function isStockDetailMarket(market: string | null | undefined): market is "kr" | "us" | "crypto" {
  return market === "kr" || market === "us" || market === "crypto";
}

export function OrderDetailCard({ order }: { order: LinkedOrder }) {
  const href =
    isStockDetailMarket(order.market) && order.symbol
      ? stockDetailPath(order.market, order.symbol)
      : null;

  return (
    <Card data-testid="order-detail-card">
      <div style={{ display: "grid", gap: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 8 }}>
          <div>
            <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
              <Pill tone={LINKED_ORDER_STATUS_TONES[order.status ?? ""] ?? "paper"}>
                {LINKED_ORDER_STATUS_LABELS[order.status ?? ""] ?? order.status ?? "—"}
              </Pill>
              <span style={{ fontSize: 20, fontWeight: 800 }}>
                {order.side === "buy" ? "매수" : order.side === "sell" ? "매도" : ""} {order.symbol ?? "—"}
              </span>
            </div>
            {href ? (
              <Link to={href} style={{ fontSize: 12, color: "var(--fg-3)" }}>
                종목 상세 보기
              </Link>
            ) : null}
          </div>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))", gap: 12 }}>
          <Field label="체결 수량">{fmtAmount(order.filledQty)}</Field>
          <Field label="평균 체결가">{fmtAmount(order.avgFillPrice)}</Field>
          <Field label="브로커">{order.broker ?? "—"}</Field>
          <Field label="계좌 구분">{order.accountScope ?? "—"}</Field>
          <Field label="주문번호">{order.orderNo ?? "—"}</Field>
          <Field label="주문 시각">{order.orderTime ?? "—"}</Field>
          <Field label="정산(reconcile) 시각">{order.reconciledAt ?? "—"}</Field>
        </div>

        {(order.thesis || order.exitReason) && (
          <div style={{ display: "grid", gap: 8 }}>
            {order.thesis && (
              <div>
                <div style={{ fontSize: 11, color: "var(--fg-3)", marginBottom: 2 }}>사유 (thesis)</div>
                <div style={{ fontSize: 13, lineHeight: 1.6 }}>{order.thesis}</div>
              </div>
            )}
            {order.exitReason && (
              <div>
                <div style={{ fontSize: 11, color: "var(--fg-3)", marginBottom: 2 }}>청산 사유</div>
                <div style={{ fontSize: 13, lineHeight: 1.6 }}>{order.exitReason}</div>
              </div>
            )}
          </div>
        )}

        {order.reportItemUuid && (
          <div style={{ fontSize: 11, color: "var(--fg-3)" }}>
            연결된 리포트 항목: {order.reportItemUuid.slice(0, 8)}…
          </div>
        )}
      </div>
    </Card>
  );
}

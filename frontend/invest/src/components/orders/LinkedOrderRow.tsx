// ROB-559 — shared linked-order status maps + row, extracted from
// InvestmentReportBundleContent (ROB-554) so the report-bundle decision log and
// the stock-detail "주문 기록" card render live orders identically.

import { Pill, type PillTone } from "../../ds";
import type { LinkedOrder } from "../../types/investmentReports";

// Covers the status vocabularies of all three live ledgers the lookups read:
// LiveOrderLedger (US/crypto), KISLiveOrderLedger (KR), and TossLiveOrderLedger
// (which adds replaced / *_rejected). Unmapped values fall back to the raw token.
export const LINKED_ORDER_STATUS_LABELS: Record<string, string> = {
  filled: "체결",
  partial: "부분체결",
  accepted: "미체결",
  submitted: "미체결",
  pending: "미체결",
  cancelled: "취소",
  rejected: "거부",
  expired: "만료",
  unknown: "확인 불가",
  replaced: "정정됨",
  cancel_rejected: "취소거부",
  replace_rejected: "정정거부",
  anomaly: "이상",
};

// Tone by status class: filled/partial → accent (executed), terminal failures
// → loss, soft-negatives → warn, everything else → neutral paper (default).
export const LINKED_ORDER_STATUS_TONES: Record<string, PillTone> = {
  filled: "accent",
  partial: "accent",
  rejected: "loss",
  cancel_rejected: "loss",
  replace_rejected: "loss",
  anomaly: "loss",
  cancelled: "warn",
  expired: "warn",
};

// ledgerId is per-table; the three live ledgers have independent id sequences,
// so disambiguate by broker+market to keep React keys unique when one container
// lists orders from more than one ledger.
export function linkedOrderKey(order: LinkedOrder): string {
  return `${order.broker ?? ""}:${order.market ?? ""}:${order.ledgerId}`;
}

// Pydantic serializes Decimal to a JSON string; sub-1e-6 magnitudes come through
// as scientific notation (e.g. "1E-8" for tiny BTC fractions). Render a readable
// fixed-point number instead, falling back to the raw value if it isn't numeric.
function fmtAmount(value: number | string | null | undefined): string {
  if (value == null || value === "") return "—";
  const n = Number(value);
  return Number.isFinite(n)
    ? n.toLocaleString(undefined, { maximumFractionDigits: 8 })
    : String(value);
}

export function LinkedOrderRow({ order }: { order: LinkedOrder }) {
  return (
    <div
      style={{
        display: "flex",
        gap: 8,
        alignItems: "baseline",
        flexWrap: "wrap",
        fontSize: 12,
        color: "var(--fg-3)",
        background: "var(--surface-2)",
        padding: "6px 10px",
        borderRadius: 8,
      }}
    >
      <Pill tone={LINKED_ORDER_STATUS_TONES[order.status ?? ""] ?? "paper"} size="sm">
        {LINKED_ORDER_STATUS_LABELS[order.status ?? ""] ?? order.status ?? "—"}
      </Pill>
      <span style={{ fontWeight: 700 }}>
        {order.side === "buy" ? "매수" : order.side === "sell" ? "매도" : ""}{" "}
        {order.symbol ?? "—"}
      </span>
      {order.filledQty != null || order.avgFillPrice != null ? (
        <span>
          {fmtAmount(order.filledQty)} @ {fmtAmount(order.avgFillPrice)}
        </span>
      ) : null}
      {order.orderTime ? <span>· {order.orderTime}</span> : null}
      {order.orderNo ? <span>· order {order.orderNo.slice(0, 8)}</span> : null}
      {order.exitReason || order.thesis ? (
        <span style={{ width: "100%" }}>{order.exitReason ?? order.thesis}</span>
      ) : null}
    </div>
  );
}

// Display formatting for the 매수 계획 board — §144차.
//
// The API sends money as exact decimal strings. Formatting needs a number, so
// these helpers parse once at the render boundary and keep the original string
// available for the `title` attribute; nothing here feeds a value back into a
// calculation, so the parse cannot move a total the operator acts on.
import { formatNumber } from "../../format/number";
import type { BuyPlanCurrency } from "../../types/buyPlan";

export function toNumber(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function money(
  value: string | null | undefined,
  currency: BuyPlanCurrency,
): string {
  const n = toNumber(value);
  if (n === null) return "-";
  return currency === "KRW"
    ? `₩${formatNumber(n, { maximumFractionDigits: 0 })}`
    : `$${formatNumber(n, { maximumFractionDigits: 2 })}`;
}

/** Price, not notional — keeps sub-won precision for cheap coins. */
export function price(
  value: string | null | undefined,
  currency: BuyPlanCurrency,
): string {
  const n = toNumber(value);
  if (n === null) return "-";
  const digits = currency === "KRW" ? (Math.abs(n) < 100 ? 2 : 0) : 2;
  const symbol = currency === "KRW" ? "₩" : "$";
  return `${symbol}${formatNumber(n, { maximumFractionDigits: digits })}`;
}

/** The API sends percent already scaled (5 means 5%), unlike formatPercent. */
export function pct(value: string | null | undefined, digits = 2): string {
  const n = toNumber(value);
  if (n === null) return "-";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(digits)}%`;
}

export function quantity(value: string | null | undefined): string {
  const n = toNumber(value);
  if (n === null) return "-";
  return formatNumber(n, { maximumFractionDigits: 8 });
}

export function dateTime(value: string | null | undefined): string {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "-";
  return parsed.toLocaleString("ko-KR", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

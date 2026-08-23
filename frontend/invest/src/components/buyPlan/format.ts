// Display formatting for the 매수 계획 board — §144차.
//
// The API sends money as exact decimal strings and this module keeps them that
// way: every helper delegates to `formatDecimalString`, which carries digits
// as strings from parse through rounding to grouping. `Number()` is
// deliberately absent — verify-r1 B5 measured `"9007199254740993"` rendering
// as `9,007,199,254,740,992` through the old `Number()` path, i.e. the
// operator saw a deposit amount one won away from the API's.
//
// Every helper returns "-" for unparseable input so a missing value never
// renders as a confident `0`.
import type { BuyPlanCurrency } from "../../types/buyPlan";
import { formatDecimalString } from "./decimal";

export const MISSING = "-";

export function money(
  value: string | null | undefined,
  currency: BuyPlanCurrency,
): string {
  const formatted = formatDecimalString(value, {
    maxFractionDigits: currency === "KRW" ? 0 : 2,
  });
  if (formatted === null) return MISSING;
  return `${currency === "KRW" ? "₩" : "$"}${formatted}`;
}

/** Price, not notional — keeps sub-won precision for cheap coins. */
export function price(
  value: string | null | undefined,
  currency: BuyPlanCurrency,
): string {
  // Two decimals are always allowed and then trimmed, so ₩1,000 stays clean
  // while a 0.42 KRW coin keeps its cents. The old version branched on
  // `Math.abs(n) < 100`, which needed a JS number.
  const formatted = formatDecimalString(value, {
    maxFractionDigits: 2,
    trimFraction: true,
  });
  if (formatted === null) return MISSING;
  return `${currency === "KRW" ? "₩" : "$"}${formatted}`;
}

/** The API sends percent already scaled (5 means 5%), unlike formatPercent. */
export function pct(value: string | null | undefined, digits = 2): string {
  const formatted = formatDecimalString(value, {
    maxFractionDigits: digits,
    explicitSign: true,
  });
  if (formatted === null) return MISSING;
  return `${formatted}%`;
}

export function quantity(value: string | null | undefined): string {
  return (
    formatDecimalString(value, { maxFractionDigits: 8, trimFraction: true }) ?? MISSING
  );
}

/** The exact string the API sent, for a `title` tooltip. */
export function exact(value: string | null | undefined): string | undefined {
  return value === null || value === undefined || value === "" ? undefined : value;
}

export function dateTime(value: string | null | undefined): string {
  if (!value) return MISSING;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return MISSING;
  return parsed.toLocaleString("ko-KR", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

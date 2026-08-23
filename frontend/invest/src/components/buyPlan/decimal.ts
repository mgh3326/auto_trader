// Arbitrary-precision decimal string formatting for the 매수 계획 board.
//
// verify-r1 B5: the API sends money as exact decimal strings, but the board
// used to render them through `Number()`. Beyond 2^53 that silently changes
// the amount — `"9007199254740993"` rendered as `9,007,199,254,740,992`, so
// the operator saw a deposit figure one won away from what the API computed.
//
// Nothing in this module converts to a JS number. Digits are carried as
// strings from parse through rounding to grouping, so the rendered value is
// exactly the value that arrived (or an explicitly rounded prefix of it).

export interface ParsedDecimal {
  negative: boolean;
  /** Integer digits, no sign, no leading zeros (at least "0"). */
  int: string;
  /** Fractional digits, no trailing-zero trimming, possibly "". */
  frac: string;
}

const DECIMAL_RE = /^([+-]?)(\d*)(?:\.(\d*))?$/;

export function parseDecimalString(raw: string | null | undefined): ParsedDecimal | null {
  if (raw === null || raw === undefined) return null;
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  const match = DECIMAL_RE.exec(trimmed);
  if (!match) return null;
  const [, sign, intRaw, fracRaw] = match;
  const int = (intRaw ?? "").replace(/^0+(?=\d)/, "");
  const frac = fracRaw ?? "";
  if (int === "" && frac === "") return null;
  return { negative: sign === "-", int: int === "" ? "0" : int, frac };
}

/** Add one unit to the last digit of a non-negative digit string. */
function increment(digits: string): string {
  const out = digits.split("");
  let i = out.length - 1;
  while (i >= 0) {
    if (out[i] === "9") {
      out[i] = "0";
      i -= 1;
      continue;
    }
    out[i] = String(Number(out[i]) + 1);
    return out.join("");
  }
  return `1${out.join("")}`;
}

/**
 * Round to `digits` fractional places, half-up on the magnitude.
 *
 * Half-up on magnitude (not toward +infinity) matches how the backend's
 * Decimal context rounds, so a value formatted here and a value formatted
 * server-side agree at the boundary.
 */
export function roundDecimal(parsed: ParsedDecimal, digits: number): ParsedDecimal {
  const places = Math.max(0, Math.trunc(digits));
  if (parsed.frac.length <= places) {
    return { ...parsed, frac: parsed.frac.padEnd(places, "0") };
  }
  const keep = parsed.frac.slice(0, places);
  const roundUp = parsed.frac.charCodeAt(places) >= 53; // '5'
  let combined = `${parsed.int}${keep}`;
  if (roundUp) combined = increment(combined);
  // `increment` can grow the string by one digit (999 -> 1000); slicing from
  // the right keeps the fractional width fixed either way.
  const frac = places === 0 ? "" : combined.slice(combined.length - places);
  const int = (places === 0 ? combined : combined.slice(0, combined.length - places)) || "0";
  return { negative: parsed.negative, int: int.replace(/^0+(?=\d)/, ""), frac };
}

function groupInteger(int: string, separator: string): string {
  let out = "";
  for (let i = 0; i < int.length; i += 1) {
    if (i > 0 && (int.length - i) % 3 === 0) out += separator;
    out += int[i];
  }
  return out;
}

export interface FormatOptions {
  /** Fractional digits to render. Extra digits are rounded away, half-up. */
  maxFractionDigits?: number;
  /** Drop trailing zeros in the fraction (prices/quantities, not money). */
  trimFraction?: boolean;
  /** Always show a leading `+` for positive values (percent deltas). */
  explicitSign?: boolean;
  separator?: string;
}

/**
 * Format an exact decimal string for display without ever touching `Number`.
 *
 * Returns `null` for input this module cannot parse, so callers render their
 * own placeholder rather than a misleading `0`.
 */
export function formatDecimalString(
  raw: string | null | undefined,
  options: FormatOptions = {},
): string | null {
  const {
    maxFractionDigits = 0,
    trimFraction = false,
    explicitSign = false,
    separator = ",",
  } = options;
  const parsed = parseDecimalString(raw);
  if (parsed === null) return null;

  const rounded = roundDecimal(parsed, maxFractionDigits);
  let frac = rounded.frac;
  if (trimFraction) frac = frac.replace(/0+$/, "");

  const body = groupInteger(rounded.int, separator) + (frac ? `.${frac}` : "");
  const isZero = rounded.int === "0" && /^0*$/.test(rounded.frac);
  // Rounding can turn -0.004 into -0; "-0" reads as a real negative amount.
  if (rounded.negative && !isZero) return `-${body}`;
  if (explicitSign && !isZero && !rounded.negative) return `+${body}`;
  return body;
}

/** Compare two decimal strings without converting either to a number. */
export function compareDecimalStrings(
  a: string | null | undefined,
  b: string | null | undefined,
): number | null {
  const left = parseDecimalString(a);
  const right = parseDecimalString(b);
  if (left === null || right === null) return null;
  if (left.negative !== right.negative) return left.negative ? -1 : 1;
  const width = Math.max(left.frac.length, right.frac.length);
  const lv = `${left.int}${left.frac.padEnd(width, "0")}`;
  const rv = `${right.int}${right.frac.padEnd(width, "0")}`;
  const padded = Math.max(lv.length, rv.length);
  const cmp = lv.padStart(padded, "0").localeCompare(rv.padStart(padded, "0"));
  return left.negative ? -cmp : cmp;
}

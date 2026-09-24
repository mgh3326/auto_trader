// Client-side mirror of app/services/manual_cash_settings.py (#671).
// The server re-validates everything; these rules exist so the operator sees
// the problem before pressing save. Keep them identical to the server.

export const MANUAL_CASH_MAX_KRW = 10_000_000_000;
export const MANUAL_CASH_CONFIRM_RATIO = 0.5;

export type KrwParse = { ok: true; value: number } | { ok: false; error: string };

// Digits only; thousands separators are tolerated only when correctly grouped
// ("1,000,000"), never as decimals. No sign, exponent, decimal point or spaces.
const PLAIN = /^\d+$/;
const GROUPED = /^\d{1,3}(,\d{3})+$/;

export function parseKrwInput(raw: string, maxKrw: number = MANUAL_CASH_MAX_KRW): KrwParse {
  const text = raw.trim();
  if (text === "") return { ok: false, error: "금액을 입력하세요" };
  if (text.startsWith("-")) return { ok: false, error: "음수는 입력할 수 없습니다" };
  if (!PLAIN.test(text) && !GROUPED.test(text)) {
    return { ok: false, error: "0 이상의 정수(원)만 입력할 수 있습니다" };
  }
  const digits = text.replace(/,/g, "");
  const value = Number(digits);
  if (!Number.isSafeInteger(value) || value > maxKrw) {
    return { ok: false, error: `상한 ${formatKrwAmount(maxKrw)}을 넘을 수 없습니다` };
  }
  return { ok: true, value };
}

export function formatKrwAmount(value: number): string {
  return `${new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 }).format(value)}원`;
}

/** |new - current| / current, or null when there is no positive baseline. */
export function changeRatio(current: number | null, next: number): number | null {
  if (current === null || !(current > 0)) return null;
  return Math.abs(next - current) / current;
}

/** >50% change, or any positive value over an absent/zero/unreadable one. */
// Integer comparison (2·|Δ| > current) so exactly +/-50% never flips on float
// rounding; the server makes the same decision with Decimal.
export function requiresLargeChangeConfirm(current: number | null, next: number): boolean {
  if (current === null || !(current > 0)) return next > 0;
  return Math.abs(next - current) * 2 > current;
}

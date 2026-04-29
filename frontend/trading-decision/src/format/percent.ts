export function formatPercent(
  v: string | number | null | undefined,
  fractionDigits = 2,
): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string" && v.length === 0) return "—";
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return String(v);
  const absStr = Math.abs(n).toFixed(fractionDigits);
  if (n > 0) return `+${absStr}%`;
  if (n < 0) return `-${absStr}%`;
  return `${absStr}%`;
}

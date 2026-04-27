export function formatDecimal(
  s: string | null | undefined,
  locale = "en-US",
  opts: Intl.NumberFormatOptions = { maximumFractionDigits: 8 },
): string {
  if (s === null || s === undefined) return "—";
  const n = Number(s);
  if (!Number.isFinite(n)) return s;
  return new Intl.NumberFormat(locale, opts).format(n);
}

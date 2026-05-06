export function formatNumber(v: number | null | undefined, opts?: Intl.NumberFormatOptions): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return new Intl.NumberFormat("ko-KR", opts).format(v);
}

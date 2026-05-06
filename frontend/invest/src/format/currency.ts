import { formatNumber } from "./number";

export function formatKrw(v: number | null | undefined): string {
  if (v === null || v === undefined) return "-";
  return `₩${formatNumber(v, { maximumFractionDigits: 0 })}`;
}

export function formatUsd(v: number | null | undefined): string {
  if (v === null || v === undefined) return "-";
  return `$${formatNumber(v, { maximumFractionDigits: 2 })}`;
}

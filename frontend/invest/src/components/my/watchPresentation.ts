// Shared presentation helpers for watch alert rows (ROB-591 panel + ROB-592
// per-symbol stock-detail card). Single source of truth so the /invest/my watch
// tab and the stock detail watch card render identically.

import type { WatchAlertRow } from "../../types/watches";

export const WATCH_STATUS_TONES: Record<string, "accent" | "warn" | "paper" | "loss"> = {
  active: "accent",
  triggered: "accent",
  expired: "warn",
  canceled: "warn",
};

export const WATCH_STATUS_LABELS: Record<string, string> = {
  active: "감시중",
  triggered: "발화됨",
  expired: "만료됨",
  canceled: "취소됨",
};

export const PROXIMITY_BAND_TONES: Record<string, "accent" | "warn" | "paper"> = {
  hit: "accent",
  within_0_5_pct: "warn",
  within_1_pct: "paper",
  outside: "paper",
};

export const PROXIMITY_BAND_LABELS: Record<string, string> = {
  hit: "도달",
  within_0_5_pct: "0.5% 이내",
  within_1_pct: "1.0% 이내",
  outside: "대기",
};

export const WATCH_MARKET_LABEL: Record<string, string> = {
  kr: "국내",
  us: "미국",
  crypto: "코인",
};

export function formatWatchMoney(value: string | number | null | undefined, market: string): string {
  if (value == null || value === "") return "—";
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return "—";

  if (market === "us") {
    return `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;
  }
  if (market === "kr") return `₩${Math.round(n).toLocaleString("ko-KR")}`;
  return `${n.toLocaleString("ko-KR", { maximumFractionDigits: 8 })}`;
}

export function formatWatchDateTime(value: string | null): string {
  if (!value) return "—";
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Seoul",
  }).format(dt);
}

export function formatWatchCondition(row: WatchAlertRow): string {
  const op = row.operator === "above" ? "이상" : row.operator === "below" ? "이하" : "범위";
  const metricName =
    row.metric === "price_above" || row.metric === "price_below" || row.metric === "price"
      ? "가격"
      : row.metric;

  if (row.operator === "between" && row.threshold_high) {
    return `${metricName} ${formatWatchMoney(row.threshold, row.market)} ~ ${formatWatchMoney(row.threshold_high, row.market)}`;
  }
  return `${metricName} ${formatWatchMoney(row.threshold, row.market)} ${op}`;
}

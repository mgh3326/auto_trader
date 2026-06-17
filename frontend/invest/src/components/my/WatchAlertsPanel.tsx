import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { fetchWatches } from "../../api/watches";
import { Pill } from "../../ds";
import { stockDetailPath } from "../../stockDetailPath";
import type {
  WatchAlertRow,
  WatchMarket,
  WatchStatus,
  WatchesResponse,
} from "../../types/watches";

const MARKET_OPTIONS: { key: WatchMarket; label: string }[] = [
  { key: "all", label: "전체" },
  { key: "kr", label: "국내" },
  { key: "us", label: "미국" },
  { key: "crypto", label: "코인" },
];

const STATUS_OPTIONS: { key: WatchStatus; label: string }[] = [
  { key: "all", label: "전체 상태" },
  { key: "active", label: "감시중" },
  { key: "triggered", label: "감시발화" },
  { key: "expired", label: "만료됨" },
  { key: "canceled", label: "취소됨" },
];

const WATCH_STATUS_TONES: Record<string, "accent" | "warn" | "paper" | "loss"> = {
  active: "accent",
  triggered: "accent",
  expired: "warn",
  canceled: "warn",
};

const WATCH_STATUS_LABELS: Record<string, string> = {
  active: "감시중",
  triggered: "발화됨",
  expired: "만료됨",
  canceled: "취소됨",
};

const PROXIMITY_BAND_TONES: Record<string, "accent" | "warn" | "paper"> = {
  hit: "accent",
  within_0_5_pct: "warn",
  within_1_pct: "paper",
  outside: "paper",
};

const PROXIMITY_BAND_LABELS: Record<string, string> = {
  hit: "도달",
  within_0_5_pct: "0.5% 이내",
  within_1_pct: "1.0% 이내",
  outside: "대기",
};

const MARKET_LABEL: Record<string, string> = {
  kr: "국내",
  us: "미국",
  crypto: "코인",
};

function formatMoney(value: string | number | null | undefined, market: string): string {
  if (value == null || value === "") return "—";
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return "—";

  if (market === "us") {
    return `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;
  }
  if (market === "kr") return `₩${Math.round(n).toLocaleString("ko-KR")}`;
  return `${n.toLocaleString("ko-KR", { maximumFractionDigits: 8 })}`;
}

function formatDateTime(value: string | null): string {
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

function formatCondition(row: WatchAlertRow): string {
  const op = row.operator === "above" ? "이상" : row.operator === "below" ? "이하" : "범위";
  const metricName = row.metric === "price_above" || row.metric === "price_below" || row.metric === "price" ? "가격" : row.metric;

  if (row.operator === "between" && row.threshold_high) {
    return `${metricName} ${formatMoney(row.threshold, row.market)} ~ ${formatMoney(row.threshold_high, row.market)}`;
  }
  return `${metricName} ${formatMoney(row.threshold, row.market)} ${op}`;
}

export function WatchAlertsPanel({ compact = false }: { compact?: boolean }) {
  const [market, setMarket] = useState<WatchMarket>("all");
  const [status, setStatus] = useState<WatchStatus>("all");
  const [state, setState] = useState<
    | { status: "loading" }
    | { status: "ready"; data: WatchesResponse }
    | { status: "error"; message: string }
  >({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    fetchWatches(market, status)
      .then((data) => {
        if (!cancelled) setState({ status: "ready", data });
      })
      .catch((err: unknown) => {
        if (!cancelled) setState({ status: "error", message: err instanceof Error ? err.message : String(err) });
      });
    return () => {
      cancelled = true;
    };
  }, [market, status]);

  const rawRows = useMemo(() => (state.status === "ready" ? state.data.items : []), [state]);
  const rows = useMemo(() => (compact ? rawRows.slice(0, 8) : rawRows), [rawRows, compact]);
  const dataState = state.status === "ready" ? state.data.data_state : null;

  return (
    <section
      data-testid="watch-alerts-panel"
      style={{
        border: "1px solid var(--border)",
        borderRadius: 16,
        background: "var(--surface)",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: compact ? "flex-start" : "center",
          justifyContent: "space-between",
          gap: 12,
          padding: compact ? "14px 14px 10px" : "16px 18px 12px",
          flexDirection: compact ? "column" : "row",
        }}
      >
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <h2 style={{ margin: 0, fontSize: compact ? 16 : 18 }}>AI 감시 트리거</h2>
            {dataState && (
              <Pill tone={dataState === "ok" ? "accent" : dataState === "degraded" ? "warn" : "loss"} size="sm">
                {dataState === "ok" ? "실시간" : dataState === "degraded" ? "시세 지연" : "확인 불가"}
              </Pill>
            )}
          </div>
          <p style={{ margin: "4px 0 0", fontSize: 12, color: "var(--fg-3)" }}>
            AI가 포착한 감시 대상과 실시간 조건 및 근접도를 표시합니다.
          </p>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignSelf: "flex-end" }}>
            {MARKET_OPTIONS.map((option) => {
              const active = market === option.key;
              return (
                <button
                  key={option.key}
                  type="button"
                  onClick={() => setMarket(option.key)}
                  style={{
                    border: "none",
                    borderRadius: 999,
                    padding: "4px 8px",
                    fontSize: 11,
                    fontWeight: 700,
                    cursor: "pointer",
                    fontFamily: "inherit",
                    background: active ? "var(--fg)" : "var(--surface-2)",
                    color: active ? "var(--bg)" : "var(--fg-2)",
                  }}
                >
                  {option.label}
                </button>
              );
            })}
          </div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignSelf: "flex-end" }}>
            {STATUS_OPTIONS.map((option) => {
              const active = status === option.key;
              return (
                <button
                  key={option.key}
                  type="button"
                  onClick={() => setStatus(option.key)}
                  style={{
                    border: "none",
                    borderRadius: 999,
                    padding: "4px 8px",
                    fontSize: 11,
                    fontWeight: 700,
                    cursor: "pointer",
                    fontFamily: "inherit",
                    background: active ? "var(--fg)" : "var(--surface-2)",
                    color: active ? "var(--bg)" : "var(--fg-2)",
                  }}
                >
                  {option.label}
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {state.status === "ready" && state.data.warnings.length > 0 && (
        <div role="alert" style={{ margin: "0 14px 12px", padding: "8px 10px", borderRadius: 10, background: "var(--warn-soft)", color: "var(--warn)", fontSize: 12 }}>
          {state.data.warnings.join(" · ")}
        </div>
      )}

      {state.status === "loading" && (
        <div style={{ padding: 24, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>감시 목록을 불러오는 중…</div>
      )}

      {state.status === "error" && (
        <div role="alert" style={{ padding: 16, color: "var(--danger)", fontSize: 13 }}>
          감시 목록을 불러오지 못했습니다. {state.message}
        </div>
      )}

      {state.status === "ready" && rows.length === 0 && (
        <div style={{ padding: 24, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>
          {state.data.empty_reason ?? "활성화된 감시 항목이 없습니다."}
        </div>
      )}

      {state.status === "ready" && rows.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", minWidth: compact ? 0 : 860 }}>
            <thead>
              <tr style={{ color: "var(--fg-3)", fontSize: 11, textAlign: "left" }}>
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>상태</th>
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>종목</th>
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>감시 조건</th>
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)", textAlign: "right" }}>현재가</th>
                {!compact && <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>인텐트 / 액션</th>}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const targetMarket = row.market;
                const href = stockDetailPath(targetMarket, row.symbol);
                const dispName = row.symbol_name && row.symbol_name !== row.symbol ? row.symbol_name : row.symbol;
                
                const symbolBlock = (
                  <>
                    <div style={{ fontSize: 13, fontWeight: 800 }}>{dispName}</div>
                    <div style={{ marginTop: 2, fontSize: 11, color: "var(--fg-3)" }}>
                      {row.symbol} · {MARKET_LABEL[row.market]}
                    </div>
                  </>
                );

                return (
                  <tr key={row.alert_uuid}>
                    <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)" }}>
                      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
                        <Pill tone={WATCH_STATUS_TONES[row.status] ?? "paper"} size="sm">
                          {WATCH_STATUS_LABELS[row.status] ?? row.status}
                        </Pill>
                        {row.near_expiry && (
                          <Pill tone="warn" size="sm">임박</Pill>
                        )}
                        {row.status === "active" && row.proximity_band && (
                          <Pill tone={PROXIMITY_BAND_TONES[row.proximity_band] ?? "paper"} size="sm">
                            {PROXIMITY_BAND_LABELS[row.proximity_band] ?? row.proximity_band}
                          </Pill>
                        )}
                      </div>
                      <div style={{ marginTop: 3, fontSize: 11, color: "var(--fg-3)" }}>
                        만료: {formatDateTime(row.valid_until)}
                      </div>
                    </td>
                    <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)" }}>
                      {href ? (
                        <Link to={href} style={{ color: "inherit", textDecoration: "none" }}>
                          {symbolBlock}
                        </Link>
                      ) : (
                        symbolBlock
                      )}
                    </td>
                    <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 13 }}>
                      <div>{formatCondition(row)}</div>
                      {!compact && row.rationale && (
                        <div style={{ marginTop: 3, fontSize: 11, color: "var(--fg-3)", maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {row.rationale}
                        </div>
                      )}
                    </td>
                    <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 13, textAlign: "right", fontFeatureSettings: '"tnum"' }}>
                      {row.current_price ? (
                        <span style={{ fontWeight: 600 }}>{formatMoney(row.current_price, row.market)}</span>
                      ) : (
                        <span style={{ color: "var(--fg-3)" }}>—</span>
                      )}
                    </td>
                    {!compact && (
                      <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)" }}>
                        <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                          <Pill tone="paper" size="sm">{row.intent}</Pill>
                          <Pill tone="paper" size="sm">{row.action_mode}</Pill>
                        </div>
                        {row.last_event && (
                          <div style={{ marginTop: 4, fontSize: 11, color: "var(--fg-3)" }}>
                            발화: {formatDateTime(row.last_event.created_at)} ({row.last_event.outcome})
                          </div>
                        )}
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div style={{ padding: "8px 14px", fontSize: 11, color: "var(--fg-3)" }}>
            총 {rawRows.length.toLocaleString("ko-KR")}건{compact && rawRows.length > 8 && " (최근 8건 표시)"}
          </div>
        </div>
      )}
    </section>
  );
}

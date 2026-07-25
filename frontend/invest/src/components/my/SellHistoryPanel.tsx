import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { fetchSellHistory } from "../../api/fills";
import { stockDetailPath } from "../../stockDetailPath";
import type { FillListResponse, FillMarket, FillRow } from "../../types/fills";

const MARKET_OPTIONS: { key: FillMarket | "all"; label: string }[] = [
  { key: "all", label: "전체" },
  { key: "kr", label: "국내" },
  { key: "us", label: "미국" },
  { key: "crypto", label: "코인" },
];

function toNumber(value: string | number | null | undefined): number | null {
  if (value == null || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatMoney(value: string | number | null | undefined, currency: string): string {
  const n = toNumber(value);
  if (n == null) return "—";
  if (currency === "USD") {
    return `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }
  if (currency === "KRW") {
    return `₩${Math.round(n).toLocaleString("ko-KR")}`;
  }
  return `${n.toLocaleString("ko-KR")} ${currency}`;
}

function formatSignedMoney(value: string | number | null | undefined, currency: string): string {
  const n = toNumber(value);
  if (n == null) return "—";
  const sign = n > 0 ? "+" : n < 0 ? "-" : "";
  return `${sign}${formatMoney(Math.abs(n), currency)}`;
}

function formatRate(value: string | number | null | undefined): string {
  const n = toNumber(value);
  if (n == null) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toLocaleString("ko-KR", { minimumFractionDigits: 1, maximumFractionDigits: 1 })}%`;
}

function signedColor(value: string | number | null | undefined): string {
  const n = toNumber(value);
  if (n == null || n === 0) return "var(--fg-2)";
  return n > 0 ? "var(--gain)" : "var(--danger)";
}

function formatQty(row: FillRow): string {
  const qty = toNumber(row.filled_qty);
  if (qty == null) return "—";
  if (row.instrument_type === "crypto") return qty.toLocaleString("ko-KR", { maximumFractionDigits: 8 });
  return `${qty.toLocaleString("ko-KR", { maximumFractionDigits: 4 })}주`;
}

function formatDateTime(value: string): string {
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

function sourceLabel(row: FillRow): string {
  if (row.source === "websocket") return "실시간";
  if (row.source === "reconciler") return "보정";
  if (row.source === "manual_import") return "수동";
  return row.source;
}

function sourceBreakdownLabel(data: FillListResponse): string | null {
  const breakdown = data.source_breakdown;
  if (!breakdown) return null;
  const parts = [
    ["실시간", breakdown.websocket],
    ["보정", breakdown.reconciler],
    ["수동", breakdown.manual_import],
  ].filter(([, count]) => Number(count) > 0);
  if (parts.length === 0) return null;
  return parts.map(([label, count]) => `${label} ${count}`).join(" · ");
}

function symbolDisplayName(row: FillRow): string | null {
  const name = row.symbol_name ?? row.symbolName;
  if (!name || name === row.symbol) return null;
  return name;
}

function totalByCurrency(rows: FillRow[]): { currency: string; total: number }[] {
  const totals = new Map<string, number>();
  for (const row of rows) {
    const notional = toNumber(row.filled_notional);
    if (notional == null) continue;
    totals.set(row.currency, (totals.get(row.currency) ?? 0) + notional);
  }
  return Array.from(totals.entries()).map(([currency, total]) => ({ currency, total }));
}

function profitByCurrency(rows: FillRow[]): { currency: string; profit: number; costBasis: number; rate: number | null }[] {
  const totals = new Map<string, { profit: number; costBasis: number }>();
  for (const row of rows) {
    const profit = toNumber(row.realized_profit);
    const costBasis = toNumber(row.cost_basis_notional);
    if (profit == null || costBasis == null) continue;
    const current = totals.get(row.currency) ?? { profit: 0, costBasis: 0 };
    current.profit += profit;
    current.costBasis += costBasis;
    totals.set(row.currency, current);
  }
  return Array.from(totals.entries()).map(([currency, value]) => ({
    currency,
    profit: value.profit,
    costBasis: value.costBasis,
    rate: value.costBasis > 0 ? (value.profit / value.costBasis) * 100 : null,
  }));
}

function routeMarket(row: FillRow): "kr" | "us" | "crypto" | null {
  if (row.instrument_type === "equity_kr") return "kr";
  if (row.instrument_type === "equity_us") return "us";
  if (row.instrument_type === "crypto") return "crypto";
  return null;
}

export function SellHistoryPanel({ compact = false }: { compact?: boolean }) {
  const [market, setMarket] = useState<FillMarket | "all">("all");
  const [state, setState] = useState<
    | { status: "loading" }
    | { status: "ready"; data: FillListResponse }
    | { status: "error"; message: string }
  >({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    fetchSellHistory({ days: 30, limit: compact ? 8 : 30, market: market === "all" ? undefined : market })
      .then((data) => {
        if (!cancelled) setState({ status: "ready", data });
      })
      .catch((err: unknown) => {
        if (!cancelled) setState({ status: "error", message: err instanceof Error ? err.message : String(err) });
      });
    return () => {
      cancelled = true;
    };
  }, [market, compact]);

  const rows = useMemo(() => (state.status === "ready" ? state.data.items : []), [state]);
  const count = state.status === "ready" ? state.data.count : 0;
  const dataState = state.status === "ready" ? state.data.data_state : null;
  const breakdownLabel = state.status === "ready" ? sourceBreakdownLabel(state.data) : null;
  const saleTotals = useMemo(() => totalByCurrency(rows), [rows]);
  const profitTotals = useMemo(() => profitByCurrency(rows), [rows]);

  return (
    <section
      data-testid="sell-history-panel"
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
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <h2 style={{ margin: 0, fontSize: compact ? 16 : 18, letterSpacing: "-0.02em" }}>매도 이력</h2>
            {dataState && (
              <span
                style={{
                  padding: "2px 7px",
                  borderRadius: 999,
                  fontSize: 11,
                  fontWeight: 700,
                  color: dataState === "fresh" ? "var(--gain)" : "var(--warn)",
                  background: dataState === "fresh" ? "var(--gain-soft)" : "var(--warn-soft)",
                }}
              >
                {dataState === "fresh" ? "최신" : dataState === "stale" ? "지연" : "대기"}
              </span>
            )}
          </div>
          <p style={{ margin: "4px 0 0", fontSize: 12, color: "var(--fg-3)" }}>
            KIS/Upbit 체결 보정 ledger 기준 최근 30일 매도 체결입니다.
          </p>
          {breakdownLabel && (
            <p style={{ margin: "4px 0 0", fontSize: 11, color: "var(--fg-3)" }}>
              출처 {breakdownLabel}
            </p>
          )}
        </div>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
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
                  padding: "6px 10px",
                  fontSize: 12,
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

      {saleTotals.length > 0 && (
        <div
          style={{
            display: "flex",
            gap: 8,
            flexWrap: "wrap",
            padding: compact ? "0 14px 12px" : "0 18px 14px",
          }}
          aria-label="매도 금액 요약"
        >
          {saleTotals.map(({ currency, total }) => (
            <div
              key={currency}
              style={{
                borderRadius: 12,
                background: "var(--surface-2)",
                padding: "8px 10px",
                minWidth: compact ? 0 : 150,
              }}
            >
              <div style={{ fontSize: 10, color: "var(--fg-3)", fontWeight: 700 }}>총 판매금액 · {currency}</div>
              <div style={{ marginTop: 2, fontSize: compact ? 13 : 15, fontWeight: 900, fontFeatureSettings: '"tnum"' }}>
                {formatMoney(total, currency)}
              </div>
            </div>
          ))}
          {profitTotals.map(({ currency, profit, rate }) => (
            <div
              key={`profit-${currency}`}
              style={{
                borderRadius: 12,
                background: "var(--surface-2)",
                padding: "8px 10px",
                minWidth: compact ? 0 : 150,
              }}
            >
              <div style={{ fontSize: 10, color: "var(--fg-3)", fontWeight: 700 }}>판매수익 · {currency}</div>
              <div style={{ marginTop: 2, fontSize: compact ? 13 : 15, fontWeight: 900, fontFeatureSettings: '"tnum"', color: signedColor(profit) }}>
                {formatSignedMoney(profit, currency)}
              </div>
              <div style={{ marginTop: 2, fontSize: 11, color: signedColor(rate) }}>수익률 {formatRate(rate)}</div>
            </div>
          ))}
          {profitTotals.length === 0 && saleTotals.length > 0 && (
            <div style={{ alignSelf: "center", fontSize: 11, color: "var(--fg-3)" }}>
              매수 원가가 매칭된 체결부터 토스 수익분석처럼 판매수익/수익률이 표시됩니다.
            </div>
          )}
        </div>
      )}

      {state.status === "loading" && (
        <div style={{ padding: 24, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>매도 이력을 불러오는 중…</div>
      )}

      {state.status === "error" && (
        <div role="alert" style={{ padding: 16, color: "var(--danger)", fontSize: 13 }}>
          매도 이력을 불러오지 못했습니다. {state.message}
        </div>
      )}

      {state.status === "ready" && rows.length === 0 && (
        <div style={{ padding: 24, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>
          {state.data.empty_reason ?? "최근 30일 매도 체결이 없습니다."}
        </div>
      )}

      {state.status === "ready" && rows.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", minWidth: compact ? 0 : 900 }}>
            <thead>
              <tr style={{ color: "var(--fg-3)", fontSize: 11, textAlign: "left" }}>
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>일시</th>
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>종목</th>
                {!compact && <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>수량</th>}
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)", textAlign: "right" }}>단가</th>
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)", textAlign: "right" }}>총 판매금액</th>
                {!compact && <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)", textAlign: "right" }}>판매수익</th>}
                {!compact && <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)", textAlign: "right" }}>수익률</th>}
                {!compact && <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>출처</th>}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const displayName = symbolDisplayName(row);
                const market = routeMarket(row);
                const href = market ? stockDetailPath(market, row.symbol) : null;
                const symbolBlock = (
                  <>
                    <div style={{ fontSize: 13, fontWeight: 800 }}>{displayName ?? row.symbol}</div>
                    <div style={{ marginTop: 2, fontSize: 11, color: "var(--fg-3)" }}>
                      {compact
                        ? `${row.symbol} · ${formatQty(row)}`
                        : `${row.symbol} · ${row.broker.toUpperCase()} · ${row.venue}`}
                    </div>
                  </>
                );

                return (
                <tr key={`${row.broker}-${row.account_mode}-${row.venue}-${row.broker_order_id}-${row.fill_seq}`}>
                  <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 12, color: "var(--fg-2)", whiteSpace: "nowrap" }}>
                    {formatDateTime(row.filled_at)}
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
                  {!compact && <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 13 }}>{formatQty(row)}</td>}
                  <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 13, textAlign: "right", fontFeatureSettings: '"tnum"' }}>
                    {formatMoney(row.filled_price, row.currency)}
                  </td>
                  <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 13, fontWeight: 800, textAlign: "right", fontFeatureSettings: '"tnum"' }}>
                    {formatMoney(row.filled_notional, row.currency)}
                  </td>
                  {!compact && (
                    <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 13, fontWeight: 800, textAlign: "right", fontFeatureSettings: '"tnum"', color: signedColor(row.realized_profit) }}>
                      {formatSignedMoney(row.realized_profit, row.currency)}
                    </td>
                  )}
                  {!compact && (
                    <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 13, fontWeight: 800, textAlign: "right", fontFeatureSettings: '"tnum"', color: signedColor(row.realized_profit_rate) }}>
                      {formatRate(row.realized_profit_rate)}
                    </td>
                  )}
                  {!compact && <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 12, color: "var(--fg-3)" }}>{sourceLabel(row)}</td>}
                </tr>
                );
              })}
            </tbody>
          </table>
          <div style={{ padding: "8px 14px", fontSize: 11, color: "var(--fg-3)" }}>
            총 {count.toLocaleString("ko-KR")}건{count > rows.length ? ` 중 ${rows.length}건 표시` : ""}
          </div>
        </div>
      )}
    </section>
  );
}

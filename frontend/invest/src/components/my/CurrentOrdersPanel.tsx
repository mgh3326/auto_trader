import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { fetchCurrentOrders } from "../../api/currentOrders";
import { Pill } from "../../ds";
import { stockDetailPath } from "../../stockDetailPath";
import {
  LINKED_ORDER_STATUS_LABELS,
  LINKED_ORDER_STATUS_TONES,
} from "../orders/LinkedOrderRow";
import type {
  CurrentOrderRow,
  CurrentOrdersMarket,
  CurrentOrdersResponse,
} from "../../types/currentOrders";

const MARKET_OPTIONS: { key: CurrentOrdersMarket; label: string }[] = [
  { key: "all", label: "전체" },
  { key: "kr", label: "국내" },
  { key: "us", label: "미국" },
  { key: "crypto", label: "코인" },
];

const BROKER_LABEL: Record<string, string> = {
  kis: "KIS",
  toss: "TOSS",
  upbit: "UPBIT",
};

const MARKET_LABEL: Record<string, string> = {
  kr: "국내",
  us: "미국",
  crypto: "코인",
};

function toNumber(value: string | number | null | undefined): number | null {
  if (value == null || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatMoney(value: string | number | null | undefined, currency: string | null): string {
  const n = toNumber(value);
  if (n == null) return "—";
  if (currency === "USD") {
    return `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }
  if (currency === "KRW") return `₩${Math.round(n).toLocaleString("ko-KR")}`;
  return `${n.toLocaleString("ko-KR")} ${currency ?? ""}`.trim();
}

function formatOrderPrice(row: CurrentOrderRow): string {
  if (row.price != null && row.price !== "") return formatMoney(row.price, row.currency);
  const orderType = row.order_type?.trim();
  if (!orderType) return "—";
  const normalized = orderType.toLowerCase();
  if (normalized === "market" || orderType.includes("시장")) return "시장가";
  return orderType;
}

function formatQty(value: string | number | null | undefined, market: string): string {
  const n = toNumber(value);
  if (n == null) return "—";
  return market === "crypto"
    ? n.toLocaleString("ko-KR", { maximumFractionDigits: 8 })
    : `${n.toLocaleString("ko-KR", { maximumFractionDigits: 4 })}주`;
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

function sideLabel(side: CurrentOrderRow["side"]): string {
  if (side === "buy") return "매수";
  if (side === "sell") return "매도";
  return "확인";
}

function symbolName(row: CurrentOrderRow): string {
  return row.symbol_name && row.symbol_name !== row.symbol ? row.symbol_name : row.symbol;
}

function sourceSummary(data: CurrentOrdersResponse): string {
  return data.sources
    .map((source) => `${BROKER_LABEL[source.broker] ?? source.broker}/${MARKET_LABEL[source.market]} ${source.count}`)
    .join(" · ");
}

export function CurrentOrdersPanel({ compact = false }: { compact?: boolean }) {
  const [market, setMarket] = useState<CurrentOrdersMarket>("all");
  const [state, setState] = useState<
    | { status: "loading" }
    | { status: "ready"; data: CurrentOrdersResponse }
    | { status: "error"; message: string }
  >({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    fetchCurrentOrders(market)
      .then((data) => {
        if (!cancelled) setState({ status: "ready", data });
      })
      .catch((err: unknown) => {
        if (!cancelled) setState({ status: "error", message: err instanceof Error ? err.message : String(err) });
      });
    return () => {
      cancelled = true;
    };
  }, [market]);

  const rows = useMemo(() => (state.status === "ready" ? state.data.items : []), [state]);
  const dataState = state.status === "ready" ? state.data.data_state : null;

  return (
    <section
      data-testid="current-orders-panel"
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
            <h2 style={{ margin: 0, fontSize: compact ? 16 : 18 }}>현재 주문</h2>
            {dataState && (
              <Pill tone={dataState === "ok" ? "accent" : dataState === "degraded" ? "warn" : "loss"} size="sm">
                {dataState === "ok" ? "정상" : dataState === "degraded" ? "부분 지연" : "확인 불가"}
              </Pill>
            )}
          </div>
          <p style={{ margin: "4px 0 0", fontSize: 12, color: "var(--fg-3)" }}>
            KIS/Toss/Upbit 라이브 API 기준 미체결·대기 주문입니다.
          </p>
          {state.status === "ready" && state.data.sources.length > 0 && (
            <p style={{ margin: "4px 0 0", fontSize: 11, color: "var(--fg-3)" }}>
              출처 {sourceSummary(state.data)}
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

      {state.status === "ready" && state.data.warnings.length > 0 && (
        <div role="alert" style={{ margin: "0 14px 12px", padding: "8px 10px", borderRadius: 10, background: "var(--warn-soft)", color: "var(--warn)", fontSize: 12 }}>
          {state.data.warnings.join(" · ")}
        </div>
      )}

      {state.status === "loading" && (
        <div style={{ padding: 24, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>현재 주문을 불러오는 중…</div>
      )}

      {state.status === "error" && (
        <div role="alert" style={{ padding: 16, color: "var(--danger)", fontSize: 13 }}>
          현재 주문을 불러오지 못했습니다. {state.message}
        </div>
      )}

      {state.status === "ready" && rows.length === 0 && (
        <div style={{ padding: 24, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>
          {state.data.empty_reason ?? "현재 미체결 주문이 없습니다."}
        </div>
      )}

      {state.status === "ready" && rows.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", minWidth: compact ? 0 : 860 }}>
            <thead>
              <tr style={{ color: "var(--fg-3)", fontSize: 11, textAlign: "left" }}>
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>주문</th>
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>종목</th>
                {!compact && <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>수량</th>}
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)", textAlign: "right" }}>가격</th>
                {!compact && <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>브로커</th>}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const href = stockDetailPath(row.market, row.symbol);
                const name = symbolName(row);
                const symbolBlock = (
                  <>
                    <div style={{ fontSize: 13, fontWeight: 800 }}>{name}</div>
                    <div style={{ marginTop: 2, fontSize: 11, color: "var(--fg-3)" }}>
                      {row.symbol} · {MARKET_LABEL[row.market]}{row.exchange ? ` · ${row.exchange}` : ""}
                    </div>
                  </>
                );

                return (
                  <tr key={`${row.broker}:${row.market}:${row.order_no}`}>
                    <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)" }}>
                      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
                        <Pill tone={LINKED_ORDER_STATUS_TONES[row.status] ?? "paper"} size="sm">
                          {LINKED_ORDER_STATUS_LABELS[row.status] ?? row.status}
                        </Pill>
                        <span style={{ fontSize: 13, fontWeight: 800 }}>{sideLabel(row.side)}</span>
                        {compact && (
                          <Pill tone={row.broker} size="sm">
                            {BROKER_LABEL[row.broker] ?? row.broker}
                          </Pill>
                        )}
                      </div>
                      <div style={{ marginTop: 3, fontSize: 11, color: "var(--fg-3)" }}>
                        {formatDateTime(row.ordered_at)} · order {row.order_no.slice(0, 8)}
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
                    {!compact && (
                      <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 13 }}>
                        {formatQty(row.remaining_qty, row.market)} / {formatQty(row.quantity, row.market)}
                      </td>
                    )}
                    <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 13, textAlign: "right", fontFeatureSettings: '"tnum"' }}>
                      {formatOrderPrice(row)}
                    </td>
                    {!compact && (
                      <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 12, color: "var(--fg-3)" }}>
                        {BROKER_LABEL[row.broker] ?? row.broker}
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div style={{ padding: "8px 14px", fontSize: 11, color: "var(--fg-3)" }}>
            총 {state.data.count.toLocaleString("ko-KR")}건
          </div>
        </div>
      )}
    </section>
  );
}

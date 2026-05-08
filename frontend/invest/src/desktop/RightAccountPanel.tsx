import type { AccountPanelResponse } from "../types/invest";
import { styleForVisual, visualBySource } from "./AccountSourceTone";

function fmtKrw(v?: number | null) {
  if (v == null) return "-";
  return `₩${Math.round(v).toLocaleString("ko-KR")}`;
}

function fmtPct(v?: number | null) {
  if (v == null) return "-";
  return `${(v * 100).toFixed(2)}%`;
}

function plColor(rate: number | null | undefined): string {
  if (rate == null) return "var(--fg-3)";
  return rate >= 0 ? "var(--gain)" : "var(--loss)";
}

export function RightAccountPanel({
  data, error, loading,
}: { data?: AccountPanelResponse; error?: string; loading?: boolean }) {
  if (loading || (!data && !error)) {
    return <div data-testid="right-panel-skeleton" style={{ padding: 16 }}>로딩 중…</div>;
  }
  if (error || !data) {
    return (
      <div data-testid="right-panel-error" style={{ padding: 16, color: "var(--danger)" }}>
        계좌 정보를 불러오지 못했습니다.{error ? ` (${error})` : ""}
      </div>
    );
  }
  const totals = data.homeSummary;
  return (
    <div data-testid="right-panel" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <section style={{ padding: 16, borderRadius: 12, background: "var(--surface)", border: "1px solid var(--border)", boxShadow: "var(--shadow-1)" }}>
        <div style={{ fontSize: 12, color: "var(--fg-3)" }}>총 자산 (KRW)</div>
        <div style={{ fontSize: 24, fontWeight: 700 }}>{fmtKrw(totals.totalValueKrw)}</div>
        <div style={{ fontSize: 12, color: plColor(totals.pnlRate) }}>
          {fmtKrw(totals.pnlKrw)} · {fmtPct(totals.pnlRate)}
        </div>
      </section>

      <section style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {data.accounts.length === 0 ? (
          <div style={{ padding: 12, color: "var(--fg-3)", fontSize: 12 }}>등록된 계좌가 없습니다.</div>
        ) : (
          data.accounts.map((a) => {
            const v = visualBySource(data.sourceVisuals, a.source);
            const style = v ? styleForVisual(v) : undefined;
            const noBalance = (a.valueKrw ?? 0) === 0 && !a.cashBalances.krw && !a.cashBalances.usd;
            return (
              <article
                key={a.accountId}
                data-testid="right-panel-account"
                data-source={a.source}
                style={{ padding: 12, borderRadius: 10, ...style }}
              >
                <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 12 }}>
                  <span>{a.displayName}</span>
                  {v && <span style={{ fontSize: 10, padding: "1px 6px", borderRadius: 4, background: "var(--surface-2)" }}>{v.badge}</span>}
                </header>
                <div style={{ fontSize: 16, fontWeight: 700, marginTop: 4 }}>{fmtKrw(a.valueKrw)}</div>
                <div style={{ fontSize: 11, color: plColor(a.pnlRate) }}>
                  {fmtKrw(a.pnlKrw)} · {fmtPct(a.pnlRate)}
                </div>
                {noBalance && <div style={{ fontSize: 11, color: "var(--fg-3)", marginTop: 4 }}>잔고 없음</div>}
              </article>
            );
          })
        )}
      </section>

      <section>
        <div style={{ fontSize: 12, color: "var(--fg-3)", marginBottom: 4 }}>관심 종목</div>
        {!data.meta.watchlistAvailable ? (
          <div style={{ fontSize: 12, color: "var(--fg-3)" }}>관심 종목 데이터를 불러올 수 없습니다.</div>
        ) : data.watchSymbols.length === 0 ? (
          <div data-testid="watchlist-empty" style={{ fontSize: 12, color: "var(--fg-3)" }}>등록된 관심 종목이 없습니다.</div>
        ) : (
          <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 4 }}>
            {data.watchSymbols.slice(0, 8).map((w) => (
              <li key={`${w.market}:${w.symbol}`} style={{ fontSize: 12 }}>
                <span style={{ color: "var(--fg-3)", marginRight: 6 }}>{w.market.toUpperCase()}</span>
                {w.displayName}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

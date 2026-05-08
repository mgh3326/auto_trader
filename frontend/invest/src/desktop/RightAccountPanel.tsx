import type { AccountPanelResponse, WatchSymbol } from "../types/invest";
import { Button, Card, Icon, PL, Pill } from "../ds";
import { pillToneForSource, visualBySource } from "./AccountSourceTone";

function fmtKrw(v?: number | null): string {
  if (v == null) return "—";
  return `₩${Math.round(v).toLocaleString("ko-KR")}`;
}

function fmtUsd(v?: number | null): string {
  if (v == null) return "—";
  return `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtPct(v?: number | null): string {
  if (v == null) return "—";
  return `${(v * 100).toFixed(2)}%`;
}

function plColor(rate: number | null | undefined): string {
  if (rate == null) return "var(--fg-3)";
  return rate >= 0 ? "var(--gain)" : "var(--loss)";
}

const MARKET_LABEL: Record<WatchSymbol["market"], string> = {
  kr: "KR",
  us: "US",
  crypto: "CRYPTO",
};

export function RightAccountPanel({
  data,
  error,
  loading,
}: {
  data?: AccountPanelResponse;
  error?: string;
  loading?: boolean;
}) {
  if (loading || (!data && !error)) {
    return (
      <div data-testid="right-panel-skeleton" style={{ padding: 16, color: "var(--fg-3)" }}>
        로딩 중…
      </div>
    );
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
    <div data-testid="right-panel" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <Card style={{ padding: 18 }}>
        <div style={{ fontSize: 12, color: "var(--fg-3)", fontWeight: 500 }}>총 자산 (KRW)</div>
        <div
          style={{
            fontSize: 26,
            fontWeight: 700,
            letterSpacing: "-0.02em",
            marginTop: 2,
            fontFeatureSettings: '"tnum"',
          }}
        >
          {fmtKrw(totals.totalValueKrw)}
        </div>
        {totals.pnlKrw != null && totals.pnlRate != null ? (
          <div style={{ marginTop: 4 }}>
            <PL value={totals.pnlKrw} pct={totals.pnlRate * 100} size={13} />
          </div>
        ) : (
          <div style={{ marginTop: 4, fontSize: 13, color: "var(--fg-3)" }}>—</div>
        )}
        <div style={{ display: "flex", gap: 6, marginTop: 14 }}>
          <Button size="sm" variant="primary" style={{ flex: 1, justifyContent: "center" }}>
            <Icon name="plus" size={14} />
            계좌 추가
          </Button>
          <Button size="sm" variant="secondary" style={{ flex: 1, justifyContent: "center" }}>
            <Icon name="refresh" size={14} />
            새로고침
          </Button>
        </div>
      </Card>

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {data.accounts.length === 0 ? (
          <div style={{ padding: 12, color: "var(--fg-3)", fontSize: 12 }}>등록된 계좌가 없습니다.</div>
        ) : (
          data.accounts.map((a) => {
            const visual = visualBySource(data.sourceVisuals, a.source);
            const tone = pillToneForSource(a.source);
            const krw = a.cashBalances.krw;
            const usd = a.cashBalances.usd;
            const noBalance = (a.valueKrw ?? 0) === 0 && !krw && !usd;
            return (
              <article
                key={a.accountId}
                data-testid="right-panel-account"
                data-source={a.source}
                style={{
                  padding: 14,
                  borderRadius: 14,
                  background: "var(--surface-2)",
                }}
              >
                <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
                  <span
                    style={{
                      fontSize: 13,
                      fontWeight: 600,
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      minWidth: 0,
                      color: "var(--fg-1)",
                    }}
                  >
                    {a.displayName}
                  </span>
                  <Pill tone={tone} size="sm">
                    {visual?.badge ?? tone.toUpperCase()}
                  </Pill>
                </header>
                <div style={{ fontSize: 18, fontWeight: 700, marginTop: 6, fontFeatureSettings: '"tnum"' }}>
                  {fmtKrw(a.valueKrw)}
                </div>
                {a.pnlKrw != null && a.pnlRate != null ? (
                  <div style={{ marginTop: 2 }}>
                    <PL value={a.pnlKrw} pct={a.pnlRate * 100} size={12} />
                  </div>
                ) : (
                  <div style={{ marginTop: 2, fontSize: 12, color: "var(--fg-3)" }}>—</div>
                )}
                {(krw != null || usd != null) && (
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: "1fr auto",
                      gap: "2px 8px",
                      fontSize: 11,
                      marginTop: 8,
                      paddingTop: 8,
                      borderTop: "1px solid var(--surface-3)",
                      color: "var(--fg-2)",
                    }}
                  >
                    {krw != null && (
                      <>
                        <span style={{ color: "var(--fg-3)" }}>원화 잔고</span>
                        <span style={{ fontFeatureSettings: '"tnum"' }}>{fmtKrw(krw)}</span>
                      </>
                    )}
                    {usd != null && (
                      <>
                        <span style={{ color: "var(--fg-3)" }}>달러 잔고</span>
                        <span style={{ fontFeatureSettings: '"tnum"' }}>{fmtUsd(usd)}</span>
                      </>
                    )}
                  </div>
                )}
                {noBalance && (
                  <div style={{ marginTop: 6, fontSize: 11, color: "var(--fg-3)" }}>잔고 없음</div>
                )}
              </article>
            );
          })
        )}
      </div>

      <Card style={{ padding: 16 }}>
        <div style={{ fontSize: 12, color: "var(--fg-3)", marginBottom: 8, fontWeight: 600 }}>관심 종목</div>
        {!data.meta.watchlistAvailable ? (
          <div style={{ fontSize: 12, color: "var(--fg-3)" }}>관심 종목 데이터를 불러올 수 없습니다.</div>
        ) : data.watchSymbols.length === 0 ? (
          <div data-testid="watchlist-empty" style={{ fontSize: 12, color: "var(--fg-3)" }}>
            등록된 관심 종목이 없습니다.
          </div>
        ) : (
          <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column" }}>
            {data.watchSymbols.slice(0, 8).map((w) => (
              <li
                key={`${w.market}:${w.symbol}`}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  padding: "8px 0",
                  borderBottom: "1px solid var(--divider)",
                }}
              >
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.3, color: "var(--fg)" }}>
                    {w.displayName}
                  </div>
                  <div style={{ fontSize: 11, color: "var(--fg-3)", fontFamily: "var(--font-mono)" }}>
                    {MARKET_LABEL[w.market]} · {w.symbol}
                  </div>
                </div>
                {w.note && <div style={{ fontSize: 11, color: "var(--fg-3)" }}>{w.note}</div>}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

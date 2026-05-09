import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAccountPanel } from "./useAccountPanel";
import { loadRecentSymbols, recordRecentSymbol } from "./recentSymbols";
import type { RecentInvestSymbol } from "./recentSymbols";
import { Button, Card, Icon, PL as ProfitLoss, Pill } from "../ds";
import type { PillTone } from "../ds";
import { fetchSignals } from "../api/signals";
import type { SignalCard } from "../types/signals";
import type { GroupedHolding, WatchSymbol } from "../types/invest";
import { formatRelativeTime } from "../format/relativeTime";

type RightPanelTab = "portfolio" | "watchlist" | "recent" | "realtime";
type RealtimeSubTab = "kr" | "us" | "crypto";
type MarketKey = "kr" | "us" | "crypto";
type NavigateToSymbol = (path: string, sym: RecentInvestSymbol) => void;

const TABS: { key: RightPanelTab; label: string }[] = [
  { key: "portfolio", label: "내 투자" },
  { key: "watchlist", label: "관심" },
  { key: "recent", label: "최근 본" },
  { key: "realtime", label: "실시간" },
];

const MARKET_LABEL: Record<MarketKey, string> = {
  kr: "KR",
  us: "US",
  crypto: "CRYPTO",
};

function fmtKrw(v?: number | null): string {
  if (v == null) return "—";
  return `₩${Math.round(v).toLocaleString("ko-KR")}`;
}

function fmtPct(v?: number | null): string {
  if (v == null) return "—";
  const pct = v * 100;
  const sign = pct >= 0 ? "+" : "";
  return `${sign}${pct.toFixed(2)}%`;
}

function TabBar({
  active,
  onChange,
}: Readonly<{
  active: RightPanelTab;
  onChange: (tab: RightPanelTab) => void;
}>) {
  return (
    <div
      role="tablist"
      style={{
        display: "flex",
        borderBottom: "1px solid var(--divider)",
        marginBottom: 12,
      }}
    >
      {TABS.map((t) => {
        const isActive = t.key === active;
        return (
          <button
            key={t.key}
            role="tab"
            aria-selected={isActive}
            onClick={() => onChange(t.key)}
            style={{
              flex: 1,
              padding: "8px 0",
              border: "none",
              borderBottom: isActive ? "2px solid var(--fg)" : "2px solid transparent",
              background: "transparent",
              color: isActive ? "var(--fg)" : "var(--fg-3)",
              fontWeight: isActive ? 700 : 500,
              fontSize: 12,
              fontFamily: "inherit",
              cursor: "pointer",
              whiteSpace: "nowrap",
            }}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

function PortfolioPanel({ onNavigate }: Readonly<{ onNavigate: NavigateToSymbol }>) {
  const { data, error, loading, refreshing, reload } = useAccountPanel();

  if (loading) {
    return (
      <div data-testid="portfolio-panel-skeleton" style={{ padding: 8, color: "var(--fg-3)", fontSize: 13 }}>
        불러오는 중…
      </div>
    );
  }

  if (error || !data) {
    return (
      <div data-testid="portfolio-panel-error" style={{ padding: 8, color: "var(--danger)", fontSize: 13 }}>
        <div>계좌 정보를 불러오지 못했습니다.</div>
        <button
          type="button"
          onClick={reload}
          style={{
            display: "block",
            marginTop: 8,
            padding: "4px 10px",
            borderRadius: 8,
            border: "1px solid var(--border)",
            background: "var(--surface)",
            color: "var(--fg-1)",
            cursor: "pointer",
            fontFamily: "inherit",
            fontSize: 12,
          }}
        >
          재시도
        </button>
      </div>
    );
  }

  const { homeSummary, groupedHoldings } = data;
  const sorted = [...groupedHoldings].sort(
    (a, b) => (b.valueKrw ?? 0) - (a.valueKrw ?? 0),
  );

  const marketKey = (m: GroupedHolding["market"]): MarketKey => {
    if (m === "KR") return "kr";
    if (m === "US") return "us";
    return "crypto";
  };

  return (
    <div data-testid="portfolio-panel" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <Card style={{ padding: 14 }}>
        <div style={{ fontSize: 11, color: "var(--fg-3)", fontWeight: 500 }}>총 자산</div>
        <div
          style={{
            fontSize: 22,
            fontWeight: 700,
            letterSpacing: "-0.02em",
            marginTop: 2,
            fontFeatureSettings: '"tnum"',
          }}
        >
          {fmtKrw(homeSummary.totalValueKrw)}
        </div>
        {homeSummary.pnlKrw != null && homeSummary.pnlRate != null ? (
          <div style={{ marginTop: 4 }}>
            <ProfitLoss value={homeSummary.pnlKrw} pct={homeSummary.pnlRate * 100} size={12} />
          </div>
        ) : (
          <div style={{ marginTop: 4, fontSize: 12, color: "var(--fg-3)" }}>—</div>
        )}
        <div style={{ display: "flex", gap: 6, marginTop: 12 }}>
          <Button
            size="sm"
            variant="secondary"
            onClick={reload}
            style={{ flex: 1, justifyContent: "center" }}
          >
            <Icon name="refresh" size={13} />
            {refreshing ? "새로고침 중…" : "새로고침"}
          </Button>
        </div>
      </Card>

      <div>
        <div style={{ fontSize: 11, fontWeight: 600, color: "var(--fg-3)", marginBottom: 6 }}>
          내 종목 현황
        </div>
        {sorted.length === 0 ? (
          <div data-testid="holdings-empty" style={{ fontSize: 12, color: "var(--fg-3)", padding: "8px 0" }}>
            보유 종목이 없습니다.
          </div>
        ) : (
          <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column" }}>
            {sorted.map((h) => {
              const mk = marketKey(h.market);
              return (
                <li key={h.groupId}>
                  <button
                    type="button"
                    onClick={() => {
                      const sym: RecentInvestSymbol = {
                        symbol: h.symbol,
                        market: mk,
                        displayName: h.displayName,
                        lastViewedAt: new Date().toISOString(),
                        source: "right-panel",
                      };
                      onNavigate(
                        `/signals?symbol=${encodeURIComponent(h.symbol)}&market=${mk}`,
                        sym,
                      );
                    }}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      padding: "8px 0",
                      width: "100%",
                      background: "transparent",
                      border: "none",
                      borderBottom: "1px solid var(--divider)",
                      cursor: "pointer",
                      textAlign: "left",
                      fontFamily: "inherit",
                    }}
                  >
                    <div style={{ minWidth: 0, flex: 1 }}>
                      <div
                        style={{
                          fontSize: 13,
                          fontWeight: 600,
                          lineHeight: 1.3,
                          color: "var(--fg)",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {h.displayName}
                      </div>
                      <div style={{ fontSize: 11, color: "var(--fg-3)", fontFamily: "var(--font-mono)" }}>
                        {MARKET_LABEL[mk]} · {h.symbol}
                      </div>
                    </div>
                    <div style={{ textAlign: "right", flexShrink: 0 }}>
                      <div style={{ fontSize: 12, fontWeight: 600, fontFeatureSettings: '"tnum"' }}>
                        {fmtKrw(h.valueKrw)}
                      </div>
                      {h.pnlRate == null ? (
                        <div style={{ fontSize: 11, color: "var(--fg-3)" }}>—</div>
                      ) : (
                        <div
                          style={{
                            fontSize: 11,
                            color: h.pnlRate >= 0 ? "var(--gain)" : "var(--loss)",
                            fontFeatureSettings: '"tnum"',
                          }}
                        >
                          {fmtPct(h.pnlRate)}
                        </div>
                      )}
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}

function WatchlistPanel({ onNavigate }: Readonly<{ onNavigate: NavigateToSymbol }>) {
  const { data, error, loading } = useAccountPanel();

  if (loading) {
    return (
      <div data-testid="watchlist-panel-skeleton" style={{ padding: 8, color: "var(--fg-3)", fontSize: 13 }}>
        불러오는 중…
      </div>
    );
  }

  if (error || !data) {
    return (
      <div data-testid="watchlist-panel-error" style={{ padding: 8, color: "var(--danger)", fontSize: 13 }}>
        관심 종목을 불러오지 못했습니다.
      </div>
    );
  }

  if (!data.meta.watchlistAvailable) {
    return (
      <div style={{ padding: 8, fontSize: 13, color: "var(--fg-3)" }}>
        관심 종목 데이터를 사용할 수 없습니다.
      </div>
    );
  }

  const { watchSymbols } = data;

  return (
    <div data-testid="watchlist-panel">
      {watchSymbols.length === 0 ? (
        <div data-testid="watchlist-panel-empty" style={{ fontSize: 13, color: "var(--fg-3)", padding: "8px 0" }}>
          등록된 관심 종목이 없습니다.
        </div>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column" }}>
          {watchSymbols.map((w) => (
            <li key={`${w.market}:${w.symbol}`}>
              <WatchRow w={w} onNavigate={onNavigate} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function WatchRow({
  w,
  onNavigate,
}: Readonly<{
  w: WatchSymbol;
  onNavigate: NavigateToSymbol;
}>) {
  return (
    <button
      type="button"
      onClick={() => {
        const sym: RecentInvestSymbol = {
          symbol: w.symbol,
          market: w.market,
          displayName: w.displayName,
          lastViewedAt: new Date().toISOString(),
          source: "right-panel",
        };
        onNavigate(
          `/feed/news?symbol=${encodeURIComponent(w.symbol)}&market=${w.market}`,
          sym,
        );
      }}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "8px 0",
        width: "100%",
        background: "transparent",
        border: "none",
        borderBottom: "1px solid var(--divider)",
        cursor: "pointer",
        textAlign: "left",
        fontFamily: "inherit",
      }}
    >
      <div style={{ minWidth: 0, flex: 1 }}>
        <div
          style={{
            fontSize: 13,
            fontWeight: 600,
            lineHeight: 1.3,
            color: "var(--fg)",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {w.displayName}
        </div>
        <div style={{ fontSize: 11, color: "var(--fg-3)", fontFamily: "var(--font-mono)" }}>
          {MARKET_LABEL[w.market]} · {w.symbol}
        </div>
      </div>
      {w.note && (
        <div style={{ fontSize: 11, color: "var(--fg-3)", flexShrink: 0, maxWidth: 60, textAlign: "right" }}>
          {w.note}
        </div>
      )}
    </button>
  );
}

function RecentPanel({
  onNavigate,
  refreshKey,
}: Readonly<{
  onNavigate: NavigateToSymbol;
  refreshKey: number;
}>) {
  const [recents, setRecents] = useState<RecentInvestSymbol[]>([]);

  useEffect(() => {
    setRecents(loadRecentSymbols());
  }, [refreshKey]);

  if (recents.length === 0) {
    return (
      <div data-testid="recent-panel-empty" style={{ fontSize: 13, color: "var(--fg-3)", padding: "8px 0" }}>
        최근 본 종목이 없습니다. 종목을 클릭하면 여기에 기록됩니다.
      </div>
    );
  }

  return (
    <ul
      data-testid="recent-panel"
      style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column" }}
    >
      {recents.map((r) => {
        const ago = formatRelativeTime(r.lastViewedAt) ?? "";
        return (
          <li key={`${r.market}:${r.symbol}`}>
            <button
              type="button"
              onClick={() => {
                onNavigate(
                  `/signals?symbol=${encodeURIComponent(r.symbol)}&market=${r.market}`,
                  { ...r, lastViewedAt: new Date().toISOString() },
                );
              }}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                padding: "8px 0",
                width: "100%",
                background: "transparent",
                border: "none",
                borderBottom: "1px solid var(--divider)",
                cursor: "pointer",
                textAlign: "left",
                fontFamily: "inherit",
              }}
            >
              <div style={{ minWidth: 0, flex: 1 }}>
                <div
                  style={{
                    fontSize: 13,
                    fontWeight: 600,
                    color: "var(--fg)",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {r.displayName}
                </div>
                <div style={{ fontSize: 11, color: "var(--fg-3)", fontFamily: "var(--font-mono)" }}>
                  {MARKET_LABEL[r.market]} · {r.symbol}
                </div>
              </div>
              {ago && (
                <div style={{ fontSize: 11, color: "var(--fg-3)", flexShrink: 0 }}>{ago}</div>
              )}
            </button>
          </li>
        );
      })}
    </ul>
  );
}

const REALTIME_TABS: { key: RealtimeSubTab; label: string }[] = [
  { key: "kr", label: "국내" },
  { key: "us", label: "해외" },
  { key: "crypto", label: "코인" },
];

const DECISION_LABEL: Record<string, string> = {
  buy: "매수",
  sell: "매도",
  hold: "보유",
  watch: "주시",
  neutral: "중립",
};

function decisionTone(decision?: string | null): PillTone {
  if (decision === "buy") return "accent";
  if (decision === "sell") return "loss";
  return "paper";
}

function RealtimePanel({
  onNavigate,
}: Readonly<{
  onNavigate: NavigateToSymbol;
}>) {
  const [subTab, setSubTab] = useState<RealtimeSubTab>("kr");
  const [items, setItems] = useState<SignalCard[]>([]);
  const [err, setErr] = useState<string | undefined>();
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancel = false;
    setLoading(true);
    setErr(undefined);
    fetchSignals({ tab: subTab, limit: 10 })
      .then((r) => {
        if (cancel) return;
        setItems(r.items);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (cancel) return;
        const msg = e instanceof Error ? e.message : String(e);
        setErr(msg);
        setLoading(false);
      });
    return () => {
      cancel = true;
    };
  }, [subTab]);

  return (
    <div data-testid="realtime-panel">
      <div style={{ fontSize: 11, color: "var(--fg-3)", marginBottom: 8 }}>
        최근 신호 (참고용, 실시간 스트림 아님)
      </div>
      <div style={{ display: "flex", gap: 4, marginBottom: 10 }}>
        {REALTIME_TABS.map((t) => {
          const on = t.key === subTab;
          return (
            <button
              key={t.key}
              type="button"
              onClick={() => setSubTab(t.key)}
              style={{
                padding: "4px 10px",
                borderRadius: 999,
                border: "1px solid var(--border)",
                background: on ? "var(--fg)" : "transparent",
                color: on ? "var(--bg)" : "var(--fg-2)",
                fontWeight: on ? 700 : 500,
                fontSize: 11,
                fontFamily: "inherit",
                cursor: "pointer",
              }}
            >
              {t.label}
            </button>
          );
        })}
      </div>

      {loading && (
        <div style={{ fontSize: 13, color: "var(--fg-3)", padding: "8px 0" }}>불러오는 중…</div>
      )}
      {err && !loading && (
        <div style={{ fontSize: 12, color: "var(--danger)" }}>오류: {err}</div>
      )}
      {!loading && !err && items.length === 0 && (
        <div data-testid="realtime-empty" style={{ fontSize: 13, color: "var(--fg-3)", padding: "8px 0" }}>
          최근 신호가 없습니다.
        </div>
      )}
      {!loading && (
        <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column" }}>
          {items.map((s) => {
            const primarySym = s.relatedSymbols[0];
            const market = (primarySym?.market ?? subTab) as MarketKey;
            const symbol = primarySym?.symbol ?? "";
            const displayName = primarySym?.displayName ?? s.title;
            const ago = formatRelativeTime(s.generatedAt) ?? "";
            const decLabel = s.decisionLabel ? (DECISION_LABEL[s.decisionLabel] ?? s.decisionLabel) : null;
            const pillTone = decisionTone(s.decisionLabel);
            return (
              <li key={s.id}>
                <button
                  type="button"
                  onClick={() => {
                    if (!symbol) return;
                    const sym: RecentInvestSymbol = {
                      symbol,
                      market,
                      displayName,
                      lastViewedAt: new Date().toISOString(),
                      source: "right-panel",
                    };
                    onNavigate(
                      `/signals?symbol=${encodeURIComponent(symbol)}&market=${market}`,
                      sym,
                    );
                  }}
                  disabled={!symbol}
                  style={{
                    display: "flex",
                    alignItems: "flex-start",
                    gap: 8,
                    padding: "8px 0",
                    width: "100%",
                    background: "transparent",
                    border: "none",
                    borderBottom: "1px solid var(--divider)",
                    cursor: symbol ? "pointer" : "default",
                    textAlign: "left",
                    fontFamily: "inherit",
                  }}
                >
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div
                      style={{
                        fontSize: 12,
                        fontWeight: 600,
                        color: "var(--fg)",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {displayName}
                    </div>
                    <div style={{ fontSize: 11, color: "var(--fg-3)", marginTop: 2 }}>
                      {symbol && <span style={{ fontFamily: "var(--font-mono)", marginRight: 4 }}>{symbol}</span>}
                      {ago}
                    </div>
                  </div>
                  {decLabel && (
                    <Pill tone={pillTone} size="sm">
                      {decLabel}
                    </Pill>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export function RightRemotePanel() {
  const [activeTab, setActiveTab] = useState<RightPanelTab>("portfolio");
  const [recentRefreshKey, setRecentRefreshKey] = useState(0);
  const navigate = useNavigate();

  const handleNavigate = useCallback(
    (path: string, sym: RecentInvestSymbol) => {
      recordRecentSymbol(sym);
      setRecentRefreshKey((k) => k + 1);
      navigate(path);
    },
    [navigate],
  );

  return (
    <div data-testid="right-remote-panel">
      <TabBar active={activeTab} onChange={setActiveTab} />
      {activeTab === "portfolio" && <PortfolioPanel onNavigate={handleNavigate} />}
      {activeTab === "watchlist" && <WatchlistPanel onNavigate={handleNavigate} />}
      {activeTab === "recent" && (
        <RecentPanel onNavigate={handleNavigate} refreshKey={recentRefreshKey} />
      )}
      {activeTab === "realtime" && <RealtimePanel onNavigate={handleNavigate} />}
    </div>
  );
}

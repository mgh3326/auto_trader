import { useCallback, useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { useAccountPanel } from "./useAccountPanel";
import { loadRecentSymbols, recordRecentSymbol } from "./recentSymbols";
import type { RecentInvestSymbol } from "./recentSymbols";
import { Button, Card, Icon, PL as ProfitLoss, Pill } from "../ds";
import type { PillTone } from "../ds";
import { fetchSignals } from "../api/signals";
import type { SignalCard } from "../types/signals";
import { buildScopedPortfolioPanel, type AccountFilterKey } from "./scopeHoldings";
import { accountSourceMeta } from "./AccountSourceMeta";
import type { GroupedHolding, WatchSymbol } from "../types/invest";
import { formatRelativeTime } from "../format/relativeTime";
import { stockDetailPath, stockDetailRouteSymbol } from "../stockDetailPath";

type RightPanelTab = "portfolio" | "watchlist" | "recent" | "realtime";
type RealtimeSubTab = "kr" | "us" | "crypto";
type MarketKey = "kr" | "us" | "crypto";
type NavigateToSymbol = (path: string, sym: RecentInvestSymbol) => void;

const PAPER_SOURCES: ReadonlySet<string> = new Set([
  "kis_mock",
  "kiwoom_mock",
  "alpaca_paper",
  "db_simulated",
]);

function isPaperSource(source: string | undefined): boolean {
  return source !== undefined && PAPER_SOURCES.has(source);
}

const TABS: { key: RightPanelTab; label: string }[] = [
  { key: "portfolio", label: "내 투자" },
  { key: "watchlist", label: "관심" },
  { key: "recent", label: "최근 본" },
  { key: "realtime", label: "실시간" },
];

const RIGHT_RAIL_TAB_STORAGE_KEY = "invest:right-rail-tab";

const RAIL_ICON_TABS: { key: RightPanelTab; label: string; icon: "chart" | "heart" | "clock" | "flash" }[] = [
  { key: "portfolio", label: "내 투자", icon: "chart" },
  { key: "watchlist", label: "관심", icon: "heart" },
  { key: "recent", label: "최근 본", icon: "clock" },
  { key: "realtime", label: "실시간", icon: "flash" },
];

function readStoredTab(): RightPanelTab {
  try {
    const value = window.localStorage.getItem(RIGHT_RAIL_TAB_STORAGE_KEY);
    if (value === "portfolio" || value === "watchlist" || value === "recent" || value === "realtime") {
      return value;
    }
  } catch {
    /* ignore */
  }
  return "portfolio";
}

function writeStoredTab(tab: RightPanelTab): void {
  try {
    window.localStorage.setItem(RIGHT_RAIL_TAB_STORAGE_KEY, tab);
  } catch {
    /* ignore */
  }
}

const MARKET_LABEL: Record<MarketKey, string> = {
  kr: "KR",
  us: "US",
  crypto: "CRYPTO",
};

function stockRouteForMarketKey(market: MarketKey, symbol: string): string {
  return `/stocks/${market}/${encodeURIComponent(stockDetailRouteSymbol(market, symbol))}`;
}

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

function fmtUsd(v?: number | null): string {
  if (v == null) return "—";
  return `$${v.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
}

const ROW_BUTTON_STYLE: CSSProperties = {
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
};

const ROW_TITLE_STYLE: CSSProperties = {
  fontSize: 13,
  fontWeight: 600,
  lineHeight: 1.3,
  color: "var(--fg)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const ROW_META_STYLE: CSSProperties = {
  fontSize: 11,
  color: "var(--fg-3)",
  fontFamily: "var(--font-mono)",
};

function SymbolRow({
  displayName,
  market,
  symbol,
  onClick,
  disabled = false,
  right,
  meta,
}: Readonly<{
  displayName: string;
  market?: MarketKey;
  symbol?: string;
  onClick: () => void;
  disabled?: boolean;
  right?: ReactNode;
  meta?: ReactNode;
}>) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      style={{
        ...ROW_BUTTON_STYLE,
        alignItems: right ? "center" : ROW_BUTTON_STYLE.alignItems,
        cursor: disabled ? "default" : "pointer",
      }}
    >
      <div style={{ minWidth: 0, flex: 1 }}>
        <div style={ROW_TITLE_STYLE}>{displayName}</div>
        <div style={ROW_META_STYLE}>
          {meta ?? (market && symbol ? `${MARKET_LABEL[market]} · ${symbol}` : symbol)}
        </div>
      </div>
      {right}
    </button>
  );
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
  const { data, error, loading, refreshing, reload, load, loadedPaperSources } = useAccountPanel();
  const [selectedAccountKey, setSelectedAccountKey] = useState<AccountFilterKey>("all");

  // Lazy load on first mount of the portfolio tab. Skip if data is already
  // loaded (e.g., the user switched away and came back).
  useEffect(() => {
    if (data === undefined && !loading && !error) {
      load({ includePaper: false });
    }
  }, [data, loading, error, load]);

  if (loading || (data === undefined && error === undefined)) {
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

  const scopedForKey = buildScopedPortfolioPanel(data, selectedAccountKey);
  const selectedKey = scopedForKey.selected.key;
  const scoped = selectedKey === selectedAccountKey
    ? scopedForKey
    : buildScopedPortfolioPanel(data, selectedKey);
  const sorted = [...scoped.groupedHoldings].sort(
    (a, b) => (b.valueKrw ?? 0) - (a.valueKrw ?? 0),
  );

  const marketKey = (m: GroupedHolding["market"]): MarketKey => {
    if (m === "KR") return "kr";
    if (m === "US") return "us";
    return "crypto";
  };

  const sectionLabel = `${scoped.selected.label} 보유종목`;
  const selectedSourceMeta = scoped.selected.source ? accountSourceMeta(scoped.selected.source) : null;
  const emptyText = selectedKey === "all"
    ? "보유 종목이 없습니다."
    : selectedSourceMeta?.tone === "paper"
      ? `${scoped.selected.label} 계좌는 표시할 모의/Paper 보유종목이 없습니다.`
      : "선택한 계좌에 표시할 보유종목이 없습니다.";
  const hasCash = scoped.cashBalances.krw != null || scoped.cashBalances.usd != null;

  return (
    <div data-testid="portfolio-panel" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <Card data-testid="account-cash-card" style={{ padding: 14 }}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "baseline" }}>
          <div style={{ fontSize: 11, color: "var(--fg-3)", fontWeight: 600 }}>선택 계좌</div>
          <div style={{ fontSize: 12, color: "var(--fg)", fontWeight: 700 }}>{scoped.selected.label}</div>
        </div>
        {hasCash ? (
          <div style={{ display: "grid", gap: 4, marginTop: 10 }}>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
              <span style={{ color: "var(--fg-3)" }}>원화 현금</span>
              <span style={{ fontWeight: 700, fontFeatureSettings: '"tnum"' }}>{fmtKrw(scoped.cashBalances.krw)}</span>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
              <span style={{ color: "var(--fg-3)" }}>달러 현금</span>
              <span style={{ fontWeight: 700, fontFeatureSettings: '"tnum"' }}>{fmtUsd(scoped.cashBalances.usd)}</span>
            </div>
          </div>
        ) : (
          <div style={{ marginTop: 10, fontSize: 12, color: "var(--fg-3)" }}>현금 정보 없음</div>
        )}
      </Card>

      <div aria-label="계좌 필터" style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        {scoped.options.map((option) => {
          const isActive = option.key === selectedKey;
          return (
            <button
              key={option.key}
              type="button"
              aria-pressed={isActive}
              onClick={() => {
                setSelectedAccountKey(option.key);
                const src = option.source;
                if (isPaperSource(src) && src !== undefined) {
                  // Only fetch if this paper source isn't already in the loaded set.
                  if (!loadedPaperSources.includes(src)) {
                    load({ includePaper: true, paperSources: [src] });
                  }
                } else if (loadedPaperSources.length > 0) {
                  // User picked a non-paper option after paper data was loaded — drop paper.
                  load({ includePaper: false });
                }
              }}
              style={{
                padding: "5px 10px",
                borderRadius: 999,
                border: "1px solid var(--border)",
                background: isActive ? "var(--fg)" : "var(--surface)",
                color: isActive ? "var(--bg)" : "var(--fg-2)",
                fontWeight: isActive ? 700 : 500,
                fontSize: 11,
                fontFamily: "inherit",
                cursor: "pointer",
              }}
            >
              {option.label}
            </button>
          );
        })}
      </div>

      <Card style={{ padding: 14 }}>
        <div style={{ fontSize: 11, color: "var(--fg-3)", fontWeight: 500 }}>투자 평가액</div>
        <div
          style={{
            fontSize: 22,
            fontWeight: 700,
            letterSpacing: "-0.02em",
            marginTop: 2,
            fontFeatureSettings: '"tnum"',
          }}
        >
          {fmtKrw(scoped.totalValueKrw)}
        </div>
        <div style={{ marginTop: 4, fontSize: 12, color: "var(--fg-3)" }}>
          투자원금 {fmtKrw(scoped.costBasisKrw)}
        </div>
        {scoped.pnlKrw != null && scoped.pnlRate != null ? (
          <div style={{ marginTop: 4 }}>
            <ProfitLoss value={scoped.pnlKrw} pct={scoped.pnlRate * 100} size={12} />
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
          {sectionLabel}
        </div>
        {sorted.length === 0 ? (
          <div data-testid="holdings-empty" style={{ fontSize: 12, color: "var(--fg-3)", padding: "8px 0" }}>
            {emptyText}
          </div>
        ) : (
          <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column" }}>
            {sorted.map((h) => {
              const mk = marketKey(h.market);
              return (
                <li key={h.groupId}>
                  <SymbolRow
                    displayName={h.displayName}
                    market={mk}
                    symbol={h.symbol}
                    onClick={() => {
                      const sym: RecentInvestSymbol = {
                        symbol: h.symbol,
                        market: mk,
                        displayName: h.displayName,
                        lastViewedAt: new Date().toISOString(),
                        source: "right-panel",
                      };
                      onNavigate(stockDetailPath(h.market, h.symbol) ?? stockRouteForMarketKey(mk, h.symbol), sym);
                    }}
                    right={(
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
                    )}
                  />
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
  const { data, error, loading, load } = useAccountPanel();

  useEffect(() => {
    if (data === undefined && !loading && !error) {
      load({ includePaper: false });
    }
  }, [data, loading, error, load]);

  if (loading || (data === undefined && error === undefined)) {
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
    <SymbolRow
      displayName={w.displayName}
      market={w.market}
      symbol={w.symbol}
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
      right={w.note ? (
        <div style={{ fontSize: 11, color: "var(--fg-3)", flexShrink: 0, maxWidth: 60, textAlign: "right" }}>
          {w.note}
        </div>
      ) : undefined}
    />
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
            <SymbolRow
              displayName={r.displayName}
              market={r.market}
              symbol={r.symbol}
              onClick={() => {
                onNavigate(stockRouteForMarketKey(r.market, r.symbol), { ...r, lastViewedAt: new Date().toISOString() });
              }}
              right={ago ? <div style={{ fontSize: 11, color: "var(--fg-3)", flexShrink: 0 }}>{ago}</div> : undefined}
            />
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
                <SymbolRow
                  displayName={displayName}
                  symbol={symbol}
                  disabled={!symbol}
                  onClick={() => {
                    if (!symbol) return;
                    const sym: RecentInvestSymbol = {
                      symbol,
                      market,
                      displayName,
                      lastViewedAt: new Date().toISOString(),
                      source: "right-panel",
                    };
                    onNavigate(stockRouteForMarketKey(market, symbol), sym);
                  }}
                  meta={(
                    <>
                      {symbol && <span style={{ fontFamily: "var(--font-mono)", marginRight: 4 }}>{symbol}</span>}
                      {ago}
                    </>
                  )}
                  right={decLabel ? <Pill tone={pillTone} size="sm">{decLabel}</Pill> : undefined}
                />
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function RailIconButton({
  label,
  icon,
  active,
  onClick,
  ariaLabel,
}: Readonly<{
  label?: string;
  icon: "chart" | "heart" | "clock" | "flash" | "expandLeft" | "settings";
  active?: boolean;
  onClick: () => void;
  ariaLabel?: string;
}>) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "true" : undefined}
      aria-label={ariaLabel ?? label}
      style={{
        width: 44,
        height: label ? 52 : 36,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 4,
        borderRadius: 10,
        color: active ? "var(--fg)" : "var(--fg-3)",
        background: "transparent",
        border: "none",
        fontFamily: "inherit",
        fontSize: 10,
        cursor: "pointer",
      }}
    >
      <span style={{ color: active ? "var(--accent)" : "currentColor" }}>
        <Icon name={icon} size={20} />
      </span>
      {label ? <span>{label}</span> : null}
    </button>
  );
}

function CollapsedRail({
  activeTab,
  onPickTab,
  onExpand,
}: Readonly<{
  activeTab: RightPanelTab;
  onPickTab: (tab: RightPanelTab) => void;
  onExpand: () => void;
}>) {
  return (
    <div
      data-testid="right-remote-panel-collapsed"
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 4,
        padding: "8px 6px",
        alignItems: "center",
      }}
    >
      <RailIconButton
        icon="expandLeft"
        ariaLabel="패널 펼치기"
        onClick={onExpand}
      />
      {RAIL_ICON_TABS.map((tab) => (
        <RailIconButton
          key={tab.key}
          label={tab.label}
          icon={tab.icon}
          active={tab.key === activeTab}
          onClick={() => onPickTab(tab.key)}
        />
      ))}
      <div style={{ height: 1, alignSelf: "stretch", background: "var(--divider)", margin: "8px 6px" }} />
      <RailIconButton icon="settings" ariaLabel="설정" onClick={() => { /* settings entry — placeholder */ }} />
    </div>
  );
}

function PaneHeader({ onCollapse }: Readonly<{ onCollapse?: () => void }>) {
  if (!onCollapse) return null;
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "flex-end",
        paddingBottom: 8,
        marginBottom: 4,
        borderBottom: "1px solid var(--divider)",
      }}
    >
      <button
        type="button"
        onClick={onCollapse}
        aria-label="패널 접기"
        data-testid="right-remote-panel-collapse"
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          padding: "4px 10px",
          borderRadius: 999,
          background: "var(--surface-2)",
          color: "var(--fg-2)",
          border: "none",
          fontFamily: "inherit",
          fontSize: 11,
          fontWeight: 600,
          cursor: "pointer",
        }}
      >
        접기
        <span
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 10,
            color: "var(--fg-3)",
          }}
        >
          ⌘.
        </span>
      </button>
    </div>
  );
}

export function RightRemotePanel({
  collapsed = false,
  onCollapseChange,
}: Readonly<{
  collapsed?: boolean;
  onCollapseChange?: (value: boolean) => void;
}> = {}) {
  const [activeTab, setActiveTabState] = useState<RightPanelTab>(() => readStoredTab());
  const [recentRefreshKey, setRecentRefreshKey] = useState(0);
  const navigate = useNavigate();

  const setActiveTab = useCallback((tab: RightPanelTab) => {
    setActiveTabState(tab);
    writeStoredTab(tab);
  }, []);

  const handleNavigate = useCallback(
    (path: string, sym: RecentInvestSymbol) => {
      recordRecentSymbol(sym);
      setRecentRefreshKey((k) => k + 1);
      navigate(path);
    },
    [navigate],
  );

  if (collapsed) {
    return (
      <div data-testid="right-remote-panel" data-collapsed="true">
        <CollapsedRail
          activeTab={activeTab}
          onPickTab={(tab) => {
            setActiveTab(tab);
            onCollapseChange?.(false);
          }}
          onExpand={() => onCollapseChange?.(false)}
        />
      </div>
    );
  }

  return (
    <div data-testid="right-remote-panel" data-collapsed="false">
      <PaneHeader onCollapse={onCollapseChange ? () => onCollapseChange(true) : undefined} />
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

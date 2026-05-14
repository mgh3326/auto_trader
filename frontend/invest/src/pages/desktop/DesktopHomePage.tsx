// /invest — home/market-entry role.
// Route contract:
//   /invest          → market entry, today overview, account summary, key navigation shortcuts.
//                      Does NOT show a full holdings ledger.
//   /invest/my       → detailed holdings/portfolio table (see DesktopPortfolioPage).
//   /invest/feed/news → news feed
//   /invest/discover  → issue discovery
//   /invest/my?tab=signals → signals inside MY
//   /invest/calendar  → earnings/events calendar
//   /invest/coverage  → data coverage dashboard
//   /invest/screener  → stock screener
//   /invest/stocks/:market/:symbol → stock detail
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { DesktopShell } from "../../desktop/DesktopShell";
import { LeftContextRail } from "../../desktop/LeftContextRail";
import type { AccountFilterKey } from "../../desktop/LeftContextRail";
import { RightRemotePanel } from "../../desktop/RightRemotePanel";
import { useInvestHome } from "../../hooks/useInvestHome";
import { useMarketDashboard } from "../../hooks/useMarketDashboard";
import { useMarketParity } from "../../hooks/useMarketParity";
import { useViewport } from "../../hooks/useViewport";
import { scopeGroupedToSource } from "../../desktop/scopeHoldings";
import { DesktopHero } from "../../components/home/DesktopHero";
import { MarketStrip, marketDashboardToStripItems } from "../../components/home/MarketStrip";
import { MarketParityStrip } from "../../components/home/MarketParityStrip";
import { MobileHomePage } from "../mobile/MobileHomePage";
import { Icon } from "../../ds";
import type { AssetCategoryKey } from "../../components/AssetCategoryFilter";
import type { AccountSource, HomeSummary } from "../../types/invest";

// Single canonical /invest home — picks the desktop or mobile renderer
// from the same data hooks based on viewport width.
export function InvestHomeRoute() {
  const viewport = useViewport();
  return viewport === "mobile" ? <MobileHomePage /> : <DesktopHomePage />;
}

interface NavCard {
  to: string;
  label: string;
  desc: string;
  icon: React.ReactNode;
}

const NAV_CARDS: NavCard[] = [
  {
    to: "/my",
    label: "내 포트폴리오",
    desc: "보유 종목 전체 목록 및 수익률",
    icon: <Icon name="person" size={20} />,
  },
  {
    to: "/feed/news",
    label: "뉴스",
    desc: "시장 뉴스 및 리서치 피드",
    icon: <Icon name="bell" size={20} />,
  },
  {
    to: "/insights",
    label: "인사이트",
    desc: "괴리·패리티 read-only 관찰",
    icon: <Icon name="chart" size={20} />,
  },
  {
    to: "/discover",
    label: "발견",
    desc: "투자 아이디어 및 이슈 탐색",
    icon: <Icon name="flash" size={20} />,
  },
  {
    to: "/my?tab=signals",
    label: "시그널",
    desc: "AI 분석 신호 및 추천",
    icon: <Icon name="chart" size={20} />,
  },
  {
    to: "/calendar",
    label: "캘린더",
    desc: "실적 발표·배당 일정",
    icon: <Icon name="calendar" size={20} />,
  },
  {
    to: "/market/fx",
    label: "FX·매크로",
    desc: "환율 경고 및 사후 검증 참고",
    icon: <Icon name="chart" size={20} />,
  },
  {
    to: "/screener",
    label: "골라보기",
    desc: "조건별 종목 필터링",
    icon: <Icon name="search" size={20} />,
  },
];

export function DesktopHomePage() {
  const home = useInvestHome();
  const market = useMarketDashboard();
  const marketParity = useMarketParity();
  const [account, setAccount] = useState<AccountFilterKey>("all");
  const [category] = useState<AssetCategoryKey>("all");

  const data = home.state.status === "ready" ? home.state.data : null;
  const marketData = market.state.status === "ready" ? market.state.data : null;
  const marketStripItems = marketDashboardToStripItems(marketData);

  const scopedGrouped = useMemo(() => {
    if (!data) return [];
    if (account === "all") return data.groupedHoldings;
    return scopeGroupedToSource(data.groupedHoldings, account as AccountSource);
  }, [data, account]);

  const summary: HomeSummary | null = useMemo(() => {
    if (!data) return null;
    if (account === "all") return data.homeSummary;
    const acct = data.accounts.find((a) => a.source === account);
    if (!acct) return data.homeSummary;
    return {
      includedSources: [acct.source],
      excludedSources: [],
      totalValueKrw: acct.valueKrw,
      costBasisKrw: acct.costBasisKrw,
      pnlKrw: acct.pnlKrw,
      pnlRate: acct.pnlRate,
    };
  }, [data, account]);

  return (
    <DesktopShell
      left={
        <LeftContextRail
          accounts={data?.accounts ?? []}
          totalKrw={data?.homeSummary.totalValueKrw ?? 0}
          account={account}
          onAccount={setAccount}
          category={category}
          onCategory={() => {}}
        />
      }
      center={
        <>
          {home.state.status === "loading" && (
            <div style={{ padding: 32, color: "var(--fg-3)", textAlign: "center" }}>불러오는 중…</div>
          )}
          {home.state.status === "error" && (
            <div style={{ padding: 16, color: "var(--danger)" }}>
              잠시 후 다시 시도해 주세요.{" "}
              <button
                type="button"
                onClick={home.reload}
                style={{
                  marginLeft: 8,
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
          )}

          {data && summary && (
            <>
              {/* Account-level summary — always summary-only, not a full ledger */}
              <DesktopHero
                summary={summary}
                accountCount={account === "all" ? data.accounts.length : 1}
                holdings={scopedGrouped}
              />

              {/* Market index strip — live data from market dashboard */}
              <MarketStrip items={marketStripItems} />

              {/* Market parity strip — read-only reference/괴리 observation, not recommendations */}
              <MarketParityStrip state={marketParity.state} reload={marketParity.reload} />

              {/* Navigation cards — market-entry shortcuts to all /invest surfaces */}
              <div>
                <h2 style={{ margin: "0 0 10px", fontSize: 15, fontWeight: 700, color: "var(--fg-2)", letterSpacing: "-0.01em" }}>
                  바로가기
                </h2>
                <div
                  data-testid="home-nav-cards"
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(3, 1fr)",
                    gap: 10,
                  }}
                >
                  {NAV_CARDS.map((card) => (
                    <Link
                      key={card.to}
                      to={card.to}
                      style={{ textDecoration: "none" }}
                    >
                      <div
                        data-testid="home-nav-card"
                        style={{
                          display: "flex",
                          alignItems: "flex-start",
                          gap: 12,
                          padding: "16px 18px",
                          background: "var(--surface)",
                          border: "1px solid var(--border)",
                          borderRadius: 14,
                          boxShadow: "var(--shadow-1)",
                          cursor: "pointer",
                          transition: "background 100ms",
                        }}
                        onMouseEnter={(e) => {
                          (e.currentTarget as HTMLDivElement).style.background = "var(--surface-2)";
                        }}
                        onMouseLeave={(e) => {
                          (e.currentTarget as HTMLDivElement).style.background = "var(--surface)";
                        }}
                      >
                        <div
                          style={{
                            width: 36,
                            height: 36,
                            borderRadius: 10,
                            background: "var(--accent-soft)",
                            color: "var(--accent-press)",
                            display: "grid",
                            placeItems: "center",
                            flexShrink: 0,
                          }}
                        >
                          {card.icon}
                        </div>
                        <div style={{ minWidth: 0 }}>
                          <div style={{ fontSize: 14, fontWeight: 700, color: "var(--fg)" }}>{card.label}</div>
                          <div
                            style={{
                              fontSize: 12,
                              color: "var(--fg-3)",
                              marginTop: 2,
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                              whiteSpace: "nowrap",
                            }}
                          >
                            {card.desc}
                          </div>
                        </div>
                      </div>
                    </Link>
                  ))}
                </div>
              </div>

              {data.meta?.warnings && data.meta.warnings.length > 0 && (
                <div
                  role="alert"
                  style={{
                    padding: "10px 14px",
                    color: "var(--warn)",
                    background: "var(--warn-soft)",
                    borderRadius: 12,
                    fontSize: 12,
                  }}
                >
                  {data.meta.warnings.map((w) => `⚠ ${w.source}: ${w.message}`).join(" · ")}
                </div>
              )}
            </>
          )}
        </>
      }
      right={<RightRemotePanel />}
    />
  );
}

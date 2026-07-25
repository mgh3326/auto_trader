// /invest/my — detailed holdings/portfolio page.
// Route contract: this is the dedicated surface for the full holdings ledger.
// The home page (/invest) shows only a summary-level hero; the full table lives here.
import { useMemo, useState } from "react";
import { DesktopShell } from "../../desktop/DesktopShell";
import { LeftContextRail } from "../../desktop/LeftContextRail";
import type { AccountFilterKey } from "../../desktop/LeftContextRail";
import { useInvestHome } from "../../hooks/useInvestHome";
import { useViewport } from "../../hooks/useViewport";
import { scopeGroupedToSource } from "../../desktop/scopeHoldings";
import { DesktopHero } from "../../components/home/DesktopHero";
import { FilterChips } from "../../components/home/FilterChips";
import { UnifiedHoldingsTable } from "../../components/my/UnifiedHoldingsTable";
import { SellHistoryPanel } from "../../components/my/SellHistoryPanel";
import { BuyHistoryPanel } from "../../components/my/BuyHistoryPanel";
import { PORTFOLIO_TABS, usePortfolioTabSearchParam, type PortfolioTab } from "../../components/my/portfolioTabs";
import { SignalsPanel } from "../../components/signals/SignalsPanel";
import { CurrentOrdersPanel } from "../../components/my/CurrentOrdersPanel";
import { WatchAlertsPanel } from "../../components/my/WatchAlertsPanel";
import { RetrospectivesPanel } from "../../components/my/RetrospectivesPanel";
import { MobilePortfolioPage } from "../mobile/MobilePortfolioPage";
import type { AssetCategoryKey } from "../../types/filters";
import type { AccountSource, HomeSummary } from "../../types/invest";

export function InvestPortfolioRoute() {
  const viewport = useViewport();
  return viewport === "mobile" ? <MobilePortfolioPage /> : <DesktopPortfolioPage />;
}

function portfolioTitle(tab: PortfolioTab): string {
  if (tab === "holdings") return "통합 보유 현황";
  if (tab === "signals") return "내 투자 시그널";
  if (tab === "currentOrders") return "현재 주문";
  if (tab === "watchAlerts") return "감시";
  if (tab === "retrospectives") return "매매 회고";
  if (tab === "buyHistory") return "매수 이력";
  return "매도 이력";
}

function portfolioDescription(tab: PortfolioTab): string {
  if (tab === "holdings") return "KIS, Toss/manual, 모의/수동 계좌를 한 화면에서 비교하고 종목별 출처를 확인합니다.";
  if (tab === "signals") return "보유·관심 종목과 시장별 AI 분석 시그널을 내 투자 화면에서 함께 확인합니다.";
  if (tab === "currentOrders") return "KIS/Toss/Upbit 실계좌의 현재 미체결·대기 주문을 읽기 전용으로 확인합니다.";
  if (tab === "watchAlerts") return "AI가 포착한 감시 대상과 실시간 조건 및 근접도를 확인합니다.";
  if (tab === "retrospectives") return "체결·회고에서 도출한 교훈과 미완료 액션을 읽기 전용으로 확인합니다.";
  if (tab === "buyHistory") return "KIS/Upbit 체결 보정 ledger 기준 최근 매수 체결을 별도 화면에서 확인합니다.";
  return "KIS/Upbit 체결 보정 ledger 기준 최근 매도 체결을 별도 화면에서 확인합니다.";
}

export function DesktopPortfolioPage() {
  const home = useInvestHome();
  const [activeTab, setActiveTab] = usePortfolioTabSearchParam();
  const [account, setAccount] = useState<AccountFilterKey>("all");
  const [category, setCategory] = useState<AssetCategoryKey>("all");

  const data = home.state.status === "ready" ? home.state.data : null;

  const scopedGrouped = useMemo(() => {
    if (!data) return [];
    if (account === "all") return data.groupedHoldings;
    return scopeGroupedToSource(data.groupedHoldings, account as AccountSource);
  }, [data, account]);

  const filteredScoped = useMemo(() => {
    return category === "all"
      ? scopedGrouped
      : scopedGrouped.filter((g) => g.assetCategory === category);
  }, [scopedGrouped, category]);

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
          onCategory={setCategory}
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
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <div style={{ fontSize: 12, color: "var(--fg-3)", fontWeight: 700 }}>내 투자</div>
                <h1 style={{ margin: 0, fontSize: 26, lineHeight: 1.2, letterSpacing: "-0.03em" }}>
                  {portfolioTitle(activeTab)}
                </h1>
                <p style={{ margin: 0, color: "var(--fg-3)", fontSize: 13 }}>
                  {portfolioDescription(activeTab)}
                </p>
              </div>
              <PortfolioTabBar activeTab={activeTab} onChange={setActiveTab} />
              {activeTab === "holdings" ? (
                <>
                  <DesktopHero
                    summary={summary}
                    accountCount={account === "all" ? data.accounts.length : 1}
                    holdings={scopedGrouped}
                  />
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 4 }}>
                    <h2 style={{ margin: 0, fontSize: 18, fontWeight: 700, letterSpacing: "-0.01em" }}>
                      통합 보유 종목 {filteredScoped.length > 0 && `(${filteredScoped.length})`}
                    </h2>
                    <FilterChips value={category} onChange={setCategory} />
                  </div>
                  <UnifiedHoldingsTable
                    holdings={filteredScoped}
                    accounts={data.accounts}
                  />
                </>
              ) : activeTab === "signals" ? (
                <SignalsPanel />
              ) : activeTab === "currentOrders" ? (
                <CurrentOrdersPanel />
              ) : activeTab === "buyHistory" ? (
                <BuyHistoryPanel />
              ) : activeTab === "watchAlerts" ? (
                <WatchAlertsPanel />
              ) : activeTab === "retrospectives" ? (
                <RetrospectivesPanel />
              ) : (
                <SellHistoryPanel />
              )}
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
    />
  );
}

function PortfolioTabBar({ activeTab, onChange }: { activeTab: PortfolioTab; onChange: (tab: PortfolioTab) => void }) {
  return (
    <div
      role="tablist"
      aria-label="내 투자 보기 전환"
      style={{
        display: "inline-flex",
        alignSelf: "flex-start",
        gap: 6,
        padding: 4,
        borderRadius: 999,
        background: "var(--surface-2)",
        border: "1px solid var(--border)",
      }}
    >
      {PORTFOLIO_TABS.map((tab) => {
        const active = activeTab === tab.key;
        return (
          <button
            key={tab.key}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(tab.key)}
            style={{
              border: "none",
              borderRadius: 999,
              padding: "8px 14px",
              fontSize: 13,
              fontWeight: 800,
              cursor: "pointer",
              fontFamily: "inherit",
              background: active ? "var(--fg)" : "transparent",
              color: active ? "var(--bg)" : "var(--fg-2)",
            }}
          >
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}

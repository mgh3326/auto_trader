import { useMemo, useState } from "react";
import { DesktopShell } from "../../desktop/DesktopShell";
import { LeftContextRail } from "../../desktop/LeftContextRail";
import type { AccountFilterKey } from "../../desktop/LeftContextRail";
import { RightAccountPanel } from "../../desktop/RightAccountPanel";
import { useAccountPanel } from "../../desktop/useAccountPanel";
import { useInvestHome } from "../../hooks/useInvestHome";
import { DesktopHero } from "../../components/home/DesktopHero";
import { MarketStrip } from "../../components/home/MarketStrip";
import { HoldingsTable } from "../../components/home/HoldingsTable";
import { FilterChips } from "../../components/home/FilterChips";
import type { AssetCategoryKey } from "../../components/AssetCategoryFilter";

export function DesktopHomePage() {
  const home = useInvestHome();
  const panel = useAccountPanel();
  const [account, setAccount] = useState<AccountFilterKey>("all");
  const [category, setCategory] = useState<AssetCategoryKey>("all");

  const data = home.state.status === "ready" ? home.state.data : null;

  const filteredGrouped = useMemo(() => {
    if (!data) return [];
    return data.groupedHoldings.filter((g) => {
      const accountMatch = account === "all" || g.includedSources.includes(account as never);
      const categoryMatch = category === "all" || g.assetCategory === category;
      return accountMatch && categoryMatch;
    });
  }, [data, account, category]);

  const summary = useMemo(() => {
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
              <DesktopHero
                summary={summary}
                accountCount={data.accounts.length}
                holdings={data.groupedHoldings}
              />
              <MarketStrip items={[]} />
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 4 }}>
                <h2 style={{ margin: 0, fontSize: 18, fontWeight: 700, letterSpacing: "-0.01em" }}>보유 종목</h2>
                <FilterChips value={category} onChange={setCategory} />
              </div>
              <HoldingsTable holdings={filteredGrouped} filter="all" />
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
      right={<RightAccountPanel data={panel.data} loading={panel.loading} error={panel.error} />}
    />
  );
}

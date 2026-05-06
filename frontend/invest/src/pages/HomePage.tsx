import { useState, useMemo } from "react";
import { AppShell } from "../components/AppShell";
import { HeroCard } from "../components/HeroCard";
import { AccountCardList } from "../components/AccountCardList";
import { AccountSelector, type AccountKey } from "../components/AccountSelector";
import { AssetCategoryFilter, type AssetCategoryKey } from "../components/AssetCategoryFilter";
import { GroupedRow, RawRow } from "../components/HoldingRow";
import { BottomNav } from "../components/BottomNav";
import { useInvestHome, type InvestHomeState } from "../hooks/useInvestHome";

export function HomePage(props?: { state?: InvestHomeState; reload?: () => void }) {
  const live = useInvestHome();
  const [account, setAccount] = useState<AccountKey>("all");
  const [category, setCategory] = useState<AssetCategoryKey>("all");
  const [showHidden, setShowHidden] = useState(false);

  const state = props?.state ?? live.state;
  const reload = props?.reload ?? live.reload;

  const data = state.status === "ready" ? state.data : null;

  const activeSummary = useMemo(() => {
    if (!data) return null;
    if (account === "all") return data.homeSummary;
    const acct = data.accounts.find((a) => a.source === account);
    if (!acct) return data.homeSummary;
    return {
      totalValueKrw: acct.valueKrw,
      costBasisKrw: acct.costBasisKrw,
      pnlKrw: acct.pnlKrw,
      pnlRate: acct.pnlRate,
      includedSources: [acct.source],
      excludedSources: [],
    };
  }, [account, data]);

  if (state.status === "loading") {
    return (
      <AppShell>
        <div className="subtle">불러오는 중…</div>
      </AppShell>
    );
  }
  if (state.status === "error") {
    return (
      <AppShell>
        <div>잠시 후 다시 시도해 주세요.</div>
        <button type="button" onClick={reload}>
          재시도
        </button>
        <div className="subtle">{state.message}</div>
      </AppShell>
    );
  }

  if (!data) return null;

  const warnings = data.meta?.warnings ?? [];
  const hiddenCounts = data.meta?.hiddenCounts;
  const hiddenHoldings = data.meta?.hiddenHoldings ?? [];
  const hasAccounts = data.accounts.length > 0;

  const filteredGrouped = data.groupedHoldings.filter(
    (g) => category === "all" || g.assetCategory === category
  );

  const filteredRaw = data.holdings.filter((h) => {
    const matchAccount = account === "all" || h.source === account;
    const matchCategory = category === "all" || h.assetCategory === category;
    return matchAccount && matchCategory;
  });

  const displayHoldings = account === "all" ? filteredGrouped : filteredRaw;

  return (
    <AppShell>
      {activeSummary && <HeroCard summary={activeSummary} />}
      <AccountSelector active={account} onChange={setAccount} />
      <AccountCardList
        accounts={
          account === "all"
            ? data.accounts
            : data.accounts.filter((a) => a.source === account)
        }
        warnings={warnings}
      />

      {warnings.length > 0 && (
        <div
          role="alert"
          style={{
            margin: "0 16px",
            padding: 8,
            color: "var(--warn)",
            fontSize: 10,
            background: "rgba(246,193,119,0.08)",
            border: "1px solid rgba(246,193,119,0.27)",
            borderRadius: 10,
          }}
        >
          {warnings.map((w) => `⚠ ${w.source}: ${w.message}`).join(" · ")}
        </div>
      )}

      {hiddenCounts && (hiddenCounts.upbitInactive > 0 || hiddenCounts.upbitDust > 0) && (
        <div
          style={{
            margin: "0 16px",
            padding: "8px 12px",
            background: "var(--surface)",
            borderRadius: 12,
            fontSize: 11,
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
          }}
        >
          <span style={{ color: "var(--muted)" }}>
            숨겨진 코인 {hiddenCounts.upbitInactive + hiddenCounts.upbitDust}개 (거래불가{" "}
            {hiddenCounts.upbitInactive} · ₩5,000 미만 {hiddenCounts.upbitDust})
          </span>
          <button
            type="button"
            onClick={() => setShowHidden(!showHidden)}
            style={{
              background: "none",
              border: "none",
              color: "var(--text)",
              fontSize: 11,
              fontWeight: 700,
              cursor: "pointer",
            }}
          >
            {showHidden ? "숨기기" : "보기"}
          </button>
        </div>
      )}

      <AssetCategoryFilter active={category} onChange={setCategory} />

      {hasAccounts ? (
        <div style={{ flex: 1, overflowY: "auto", paddingBottom: 80 }}>
          {displayHoldings.length > 0 ? (
            displayHoldings.map((h) =>
              "groupId" in h ? (
                <GroupedRow key={h.groupId} row={h} />
              ) : (
                <RawRow key={h.holdingId} row={h} />
              )
            )
          ) : (
            <div style={{ padding: 40, textAlign: "center", color: "var(--muted)", fontSize: 13 }}>
              해당 조건에 보유 종목이 없습니다.
            </div>
          )}

          {showHidden && hiddenHoldings.length > 0 && (
            <div style={{ marginTop: 16 }}>
              <div style={{ padding: "0 16px 8px", fontSize: 11, color: "var(--muted)" }}>
                숨겨진 보유 종목
              </div>
              {hiddenHoldings.map((h) => (
                <RawRow key={h.holdingId} row={h} />
              ))}
            </div>
          )}
        </div>
      ) : (
        <div
          style={{
            margin: "0 16px",
            padding: 18,
            border: "1px solid var(--surface-2)",
            borderRadius: 18,
            background: "var(--surface)",
          }}
        >
          <div style={{ fontWeight: 800, marginBottom: 8 }}>연결된 계좌가 없습니다</div>
          <div className="subtle" style={{ marginBottom: 12 }}>
            기존 포트폴리오 화면에서 계좌 또는 수동 보유를 먼저 확인해 주세요.
          </div>
          <a href="/portfolio/" style={{ color: "var(--gain)", fontWeight: 700 }}>
            포트폴리오로 이동
          </a>
        </div>
      )}
      <BottomNav />
    </AppShell>
  );
}

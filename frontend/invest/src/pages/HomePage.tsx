import { useState } from "react";
import { AppShell } from "../components/AppShell";
import { HeroCard } from "../components/HeroCard";
import { AccountCardList } from "../components/AccountCardList";
import { SourceFilterBar, type ActiveSource } from "../components/SourceFilterBar";
import { GroupedRow, RawRow } from "../components/HoldingRow";
import { BottomNav } from "../components/BottomNav";
import { useInvestHome, type InvestHomeState } from "../hooks/useInvestHome";

const FILTER_SOURCES: ActiveSource[] = ["all", "kis", "upbit", "toss_manual"];

export function HomePage(props?: { state?: InvestHomeState; reload?: () => void }) {
  const live = useInvestHome();
  const [active, setActive] = useState<ActiveSource>("all");

  const state = props?.state ?? live.state;
  const reload = props?.reload ?? live.reload;

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

  const { data } = state;
  const warnings = data.meta?.warnings ?? [];

  return (
    <AppShell>
      <HeroCard summary={data.homeSummary} />
      <AccountCardList accounts={data.accounts} />
      <SourceFilterBar sources={FILTER_SOURCES} active={active} onChange={setActive} />
      {warnings.length > 0 && (
        <div
          role="alert"
          style={{
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
      <div style={{ flex: 1, overflowY: "auto" }}>
        {active === "all"
          ? data.groupedHoldings.map((g) => <GroupedRow key={g.groupId} row={g} />)
          : data.holdings
              .filter((h) => h.source === active)
              .map((h) => <RawRow key={h.holdingId} row={h} />)}
      </div>
      <BottomNav />
    </AppShell>
  );
}

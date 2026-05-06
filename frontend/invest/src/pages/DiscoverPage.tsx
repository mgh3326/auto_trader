// frontend/invest/src/pages/DiscoverPage.tsx
import { AppShell } from "../components/AppShell";
import { BottomNav } from "../components/BottomNav";
import { AiIssueCard } from "../components/discover/AiIssueCard";
import { AiIssueTicker } from "../components/discover/AiIssueTicker";
import { CategoryShortcutRail } from "../components/discover/CategoryShortcutRail";
import { DiscoverHeader } from "../components/discover/DiscoverHeader";
import { TodayEventCard } from "../components/discover/TodayEventCard";
import {
  countByRiskCategory,
  relatedNewsCount,
  sortIssueItems,
} from "../components/discover/severity";
import { useNewsRadar, type NewsRadarState } from "../hooks/useNewsRadar";

export interface DiscoverPageProps {
  state?: NewsRadarState;
  reload?: () => void;
}

export function DiscoverPage(props: DiscoverPageProps = {}) {
  const live = useNewsRadar({
    market: "all",
    hours: 24,
    includeExcluded: true,
    limit: 20,
  });
  const state = props.state ?? live.state;
  const reload = props.reload ?? live.reload;

  if (state.status === "loading") {
    return (
      <AppShell>
        <div className="subtle">불러오는 중…</div>
        <BottomNav />
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
        <BottomNav />
      </AppShell>
    );
  }

  const { data } = state;
  const sorted = sortIssueItems(data.items);
  const buckets = countByRiskCategory(data.items);
  const isStale = data.readiness.status === "stale";

  return (
    <AppShell>
      <DiscoverHeader />
      <CategoryShortcutRail />
      <TodayEventCard />
      <AiIssueTicker asOf={data.as_of} />
      {isStale && (
        <div
          role="status"
          style={{
            padding: 8,
            background: "rgba(246,193,119,0.08)",
            border: "1px solid rgba(246,193,119,0.27)",
            color: "var(--warn)",
            borderRadius: 10,
            fontSize: 11,
          }}
        >
          데이터가 최신이 아닐 수 있습니다.
        </div>
      )}
      {sorted.length === 0 ? (
        <div className="subtle">표시할 이슈가 없습니다.</div>
      ) : (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 10,
            flex: 1,
            overflowY: "auto",
          }}
        >
          {sorted.map((item, idx) => (
            <AiIssueCard
              key={item.id}
              rank={idx + 1}
              item={item}
              relatedCount={relatedNewsCount(item, buckets)}
            />
          ))}
        </div>
      )}
      <BottomNav />
    </AppShell>
  );
}

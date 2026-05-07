// frontend/invest/src/pages/DiscoverPage.tsx
import { AppShell } from "../components/AppShell";
import { BottomNav } from "../components/BottomNav";
import { AiIssueCard } from "../components/discover/AiIssueCard";
import { AiIssueTicker } from "../components/discover/AiIssueTicker";
import { CategoryShortcutRail } from "../components/discover/CategoryShortcutRail";
import { DiscoverHeader } from "../components/discover/DiscoverHeader";
import { TodayEventCard } from "../components/discover/TodayEventCard";
import { sortMarketIssues } from "../components/discover/severity";
import { useNewsIssues, type NewsIssuesState } from "../hooks/useNewsIssues";

export interface DiscoverPageProps {
  state?: NewsIssuesState;
  reload?: () => void;
}

export function DiscoverPage(props: DiscoverPageProps = {}) {
  const live = useNewsIssues(
    {
      market: "all",
      windowHours: 24,
      limit: 20,
    },
    { enabled: props.state === undefined },
  );
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
  const sorted = sortMarketIssues(data.items);

  return (
    <AppShell>
      <DiscoverHeader />
      <CategoryShortcutRail />
      <TodayEventCard />
      <AiIssueTicker asOf={data.as_of} windowHours={data.window_hours} />
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
          {sorted.map((issue) => (
            <AiIssueCard key={issue.id} issue={issue} />
          ))}
        </div>
      )}
      <BottomNav />
    </AppShell>
  );
}

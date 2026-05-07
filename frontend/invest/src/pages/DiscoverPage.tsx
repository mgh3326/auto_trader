// frontend/invest/src/pages/DiscoverPage.tsx
import { AppShell } from "../components/AppShell";
import { BottomNav } from "../components/BottomNav";
import { AiIssueCard } from "../components/discover/AiIssueCard";
import { AiIssueTicker } from "../components/discover/AiIssueTicker";
import { CategoryShortcutRail } from "../components/discover/CategoryShortcutRail";
import { DiscoverCalendarCard } from "../components/discover/DiscoverCalendarCard";
import { DiscoverHeader } from "../components/discover/DiscoverHeader";
import { sortMarketIssues } from "../components/discover/severity";
import { useNewsIssues, type NewsIssuesState } from "../hooks/useNewsIssues";

export interface DiscoverPageProps {
  state?: NewsIssuesState;
  reload?: () => void;
  /** Override "today" for SSR/tests; defaults to client clock. */
  today?: string;
}

function isoWeekRange(today: string): { fromDate: string; toDate: string } {
  const d = new Date(today + "T00:00:00");
  const day = d.getUTCDay(); // 0 = Sun
  const mondayOffset = day === 0 ? -6 : 1 - day;
  const monday = new Date(d);
  monday.setUTCDate(d.getUTCDate() + mondayOffset);
  const sunday = new Date(monday);
  sunday.setUTCDate(monday.getUTCDate() + 6);
  const iso = (x: Date) => x.toISOString().slice(0, 10);
  return { fromDate: iso(monday), toDate: iso(sunday) };
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

  const today = props.today ?? new Date().toISOString().slice(0, 10);
  const { fromDate, toDate } = isoWeekRange(today);

  let issueSection;
  if (state.status === "loading") {
    issueSection = <div className="subtle">AI 실시간 이슈를 불러오는 중…</div>;
  } else if (state.status === "error") {
    issueSection = (
      <div>
        <div>AI 실시간 이슈를 잠시 후 다시 시도해 주세요.</div>
        <button type="button" onClick={reload}>
          재시도
        </button>
        <div className="subtle">{state.message}</div>
      </div>
    );
  } else {
    const { data } = state;
    const sorted = sortMarketIssues(data.items);
    issueSection = (
      <>
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
      </>
    );
  }

  return (
    <AppShell>
      <DiscoverHeader />
      <CategoryShortcutRail />
      <DiscoverCalendarCard fromDate={fromDate} toDate={toDate} today={today} />
      {issueSection}
      <BottomNav />
    </AppShell>
  );
}

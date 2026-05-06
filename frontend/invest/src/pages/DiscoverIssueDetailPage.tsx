// frontend/invest/src/pages/DiscoverIssueDetailPage.tsx
import { Link, useParams } from "react-router-dom";
import { AppShell } from "../components/AppShell";
import { BottomNav } from "../components/BottomNav";
import { IssueImpactMap } from "../components/discover/IssueImpactMap";
import { RelatedSymbolsList } from "../components/discover/RelatedSymbolsList";
import { describeSeverity } from "../components/discover/severity";
import { formatRelativeTime } from "../format/relativeTime";
import { useNewsRadar, type NewsRadarState } from "../hooks/useNewsRadar";

export interface DiscoverIssueDetailPageProps {
  state?: NewsRadarState;
  reload?: () => void;
}

export function DiscoverIssueDetailPage(props: DiscoverIssueDetailPageProps = {}) {
  const params = useParams<{ issueId: string }>();
  const live = useNewsRadar({
    market: "all",
    hours: 24,
    includeExcluded: true,
    limit: 20,
  });
  const state = props.state ?? live.state;
  const reload = props.reload ?? live.reload;
  const issueId = params.issueId ?? "";

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

  const item = state.data.items.find((i) => i.id === issueId);
  if (!item) {
    return (
      <AppShell>
        <div style={{ padding: 16 }}>
          <p>이슈를 찾을 수 없습니다. 시간이 지나 목록에서 빠졌을 수 있어요.</p>
          <Link
            to="/discover"
            style={{ color: "var(--accent, #7eb6ff)", fontWeight: 700 }}
          >
            발견으로 돌아가기
          </Link>
        </div>
        <BottomNav />
      </AppShell>
    );
  }

  const indicator = describeSeverity(item.severity);
  const time = formatRelativeTime(item.published_at);

  return (
    <AppShell>
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <Link to="/discover" className="subtle" style={{ textDecoration: "none" }}>
          ← 발견
        </Link>
        <header style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span
              aria-label={indicator.label}
              role="img"
              style={{ color: indicator.color }}
            >
              {indicator.glyph}
            </span>
            <h1 style={{ margin: 0, fontSize: 18, fontWeight: 800 }}>{item.title}</h1>
          </div>
          {item.snippet && (
            <p className="subtle" style={{ margin: 0 }}>{item.snippet}</p>
          )}
          <div className="subtle" style={{ display: "flex", gap: 8, fontSize: 11 }}>
            {item.source && <span>{item.source}</span>}
            {time && <span>· {time}</span>}
          </div>
        </header>
        <IssueImpactMap category={item.risk_category} />
        <RelatedSymbolsList symbols={item.symbols} />
        <section
          style={{
            marginTop: 16,
            padding: 12,
            background: "var(--surface)",
            border: "1px solid var(--surface-2)",
            borderRadius: 12,
            fontSize: 12,
          }}
        >
          <strong style={{ display: "block", marginBottom: 4 }}>꼭 알아두세요</strong>
          <span className="subtle">
            이 화면은 read-only 정보입니다. 매수/매도 주문이나 자동 추천을 제공하지 않습니다.
          </span>
        </section>
      </div>
      <BottomNav />
    </AppShell>
  );
}

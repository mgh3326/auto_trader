import { useState } from "react";
import { MobileShell } from "../../mobile/MobileShell";
import { useNewsIssues } from "../../hooks/useNewsIssues";
import { sortMarketIssues } from "../../components/discover/severity";
import { IssueCard } from "../../components/discover/IssueCard";

export function MobileDiscoverPage() {
  const issues = useNewsIssues({ market: "all", windowHours: 24, limit: 20 });
  const [openId, setOpenId] = useState<string | null>(null);

  return (
    <MobileShell title="발견">
      <div data-testid="discover-issues" style={{ padding: "14px 16px", display: "flex", flexDirection: "column", gap: 10 }}>
        <p style={{ margin: 0, fontSize: 12, color: "var(--fg-3)" }}>
          뉴스 기반 참고 정보입니다. 매매 추천이 아닙니다.
        </p>

        {issues.state.status === "loading" && (
          <div style={{ color: "var(--fg-3)" }}>AI 실시간 이슈를 불러오는 중…</div>
        )}
        {issues.state.status === "error" && (
          <div style={{ color: "var(--danger)" }}>
            잠시 후 다시 시도해 주세요.{" "}
            <button
              type="button"
              onClick={issues.reload}
              style={{
                marginLeft: 8,
                padding: "2px 8px",
                borderRadius: 6,
                border: "1px solid var(--border)",
                background: "var(--surface)",
                color: "var(--fg-1)",
                cursor: "pointer",
                fontFamily: "inherit",
                fontSize: 11,
              }}
            >
              재시도
            </button>
          </div>
        )}
        {issues.state.status === "ready" && (() => {
          const sorted = sortMarketIssues(issues.state.data.items);
          if (sorted.length === 0) {
            return <div style={{ color: "var(--fg-3)" }}>표시할 이슈가 없습니다.</div>;
          }
          return (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {sorted.map((issue) => (
                <IssueCard
                  key={issue.id}
                  issue={issue}
                  expanded={openId === issue.id}
                  onToggle={() => setOpenId(openId === issue.id ? null : issue.id)}
                />
              ))}
            </div>
          );
        })()}
      </div>
    </MobileShell>
  );
}

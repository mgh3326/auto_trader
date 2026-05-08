import { useState } from "react";
import { DesktopShell } from "../../desktop/DesktopShell";
import { RightAccountPanel } from "../../desktop/RightAccountPanel";
import { useAccountPanel } from "../../desktop/useAccountPanel";
import { useViewport } from "../../hooks/useViewport";
import { useNewsIssues } from "../../hooks/useNewsIssues";
import { sortMarketIssues } from "../../components/discover/severity";
import { IssueCard } from "../../components/discover/IssueCard";
import { Card, Icon, Pill } from "../../ds";
import { MobileDiscoverPage } from "../mobile/MobileDiscoverPage";

export function InvestDiscoverRoute() {
  return useViewport() === "mobile" ? <MobileDiscoverPage /> : <DesktopDiscoverPage />;
}

export function DesktopDiscoverPage() {
  const panel = useAccountPanel();
  const issues = useNewsIssues({ market: "all", windowHours: 24, limit: 20 });
  const [openId, setOpenId] = useState<string | null>(null);

  return (
    <DesktopShell
      center={
        <>
          <header style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 16 }}>
            <div>
              <h1 style={{ margin: 0, fontSize: 22, fontWeight: 800, letterSpacing: "-0.02em" }}>발견</h1>
              <p style={{ margin: "4px 0 0", fontSize: 13, color: "var(--fg-3)" }}>
                뉴스 기반 참고 정보입니다. 매매 추천이 아닙니다.
              </p>
            </div>
            {issues.state.status === "ready" && (
              <Pill tone="accent">실시간 · {issues.state.data.window_hours}시간 윈도우</Pill>
            )}
          </header>

          <Card soft padded style={{ padding: 14, display: "flex", alignItems: "center", gap: 10 }}>
            <Icon name="info" size={18} />
            <span style={{ fontSize: 13, color: "var(--fg-2)" }}>
              AI가 동시에 보도되는 키워드를 묶어 시장 이슈를 실시간으로 정렬합니다.
            </span>
          </Card>

          <div data-testid="discover-issues">
            {issues.state.status === "loading" && (
              <div style={{ padding: 16, color: "var(--fg-3)" }}>AI 실시간 이슈를 불러오는 중…</div>
            )}
            {issues.state.status === "error" && (
              <div style={{ padding: 16, color: "var(--danger)" }}>
                AI 실시간 이슈를 잠시 후 다시 시도해 주세요.{" "}
                <button
                  type="button"
                  onClick={issues.reload}
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
                <div style={{ fontSize: 12, color: "var(--fg-3)", marginTop: 4 }}>{issues.state.message}</div>
              </div>
            )}
            {issues.state.status === "ready" && (() => {
              const sorted = sortMarketIssues(issues.state.data.items);
              if (sorted.length === 0) {
                return <div style={{ padding: 16, color: "var(--fg-3)" }}>표시할 이슈가 없습니다.</div>;
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
        </>
      }
      right={<RightAccountPanel data={panel.data} loading={panel.loading} error={panel.error} onRefresh={panel.reload} />}
    />
  );
}

import { useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import ErrorView from "../components/ErrorView";
import LoadingView from "../components/LoadingView";
import ResearchFundamentalsTab from "../components/ResearchFundamentalsTab";
import ResearchMarketTab from "../components/ResearchMarketTab";
import ResearchNewsTab from "../components/ResearchNewsTab";
import ResearchRawTab from "../components/ResearchRawTab";
import ResearchSocialTab from "../components/ResearchSocialTab";
import ResearchSummaryTab from "../components/ResearchSummaryTab";
import ResearchTabs from "../components/ResearchTabs";
import { useResearchSession } from "../hooks/useResearchSession";
import {
  RESEARCH_SESSION_STATUS_LABEL,
  RESEARCH_TAB_LABEL,
} from "../i18n/ko";
import type { StageType } from "../api/types";
import styles from "./ResearchSessionDetailPage.module.css";

const TABS = [
  { id: "summary", label: RESEARCH_TAB_LABEL.summary },
  { id: "market", label: RESEARCH_TAB_LABEL.market },
  { id: "news", label: RESEARCH_TAB_LABEL.news },
  { id: "fundamentals", label: RESEARCH_TAB_LABEL.fundamentals },
  { id: "social", label: RESEARCH_TAB_LABEL.social },
  { id: "raw", label: RESEARCH_TAB_LABEL.raw },
] as const;

export default function ResearchSessionDetailPage() {
  const params = useParams<{ sessionId: string }>();
  const sessionId = Number(params.sessionId);
  const validId = Number.isFinite(sessionId) && sessionId > 0;
  const state = useResearchSession(validId ? sessionId : 0);
  const [active, setActive] = useState<string>("summary");

  const stagesByType = useMemo(() => {
    const map: Partial<Record<StageType, NonNullable<typeof state.data>["stages"][number]>> = {};
    for (const stage of state.data?.stages ?? []) {
      map[stage.stage_type] = stage;
    }
    return map;
  }, [state.data]);

  if (!validId) return <ErrorView message="잘못된 세션 ID 입니다." />;
  if (state.status === "loading" || state.status === "idle")
    return <LoadingView />;
  if (state.status === "not_found")
    return <ErrorView message="세션을 찾을 수 없습니다." />;
  if (state.status === "error" || !state.data)
    return <ErrorView message={state.error ?? "오류가 발생했습니다."} />;

  const session = state.data.session;
  const sessionStatusKey = session.status as keyof typeof RESEARCH_SESSION_STATUS_LABEL;
  const statusLabel =
    RESEARCH_SESSION_STATUS_LABEL[sessionStatusKey] ?? session.status;

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1>
          {session.symbol ?? `세션 #${session.id}`}{" "}
          <small>{session.instrument_type ?? ""}</small>
        </h1>
        <p>
          상태: <span data-status={session.status}>{statusLabel}</span>
          {session.started_at ? ` · 시작 ${session.started_at}` : ""}
          {session.finalized_at ? ` · 완료 ${session.finalized_at}` : ""}
        </p>
        {state.data.summary?.warnings?.length ? (
          <div role="alert" className={styles.banner}>
            경고: {state.data.summary.warnings.join(", ")}
          </div>
        ) : null}
      </header>

      <ResearchTabs
        tabs={[...TABS]}
        activeId={active}
        onChange={setActive}
        renderPanel={(id) => {
          if (id === "summary")
            return (
              <ResearchSummaryTab data={state.data!} onJumpToStage={setActive} />
            );
          if (id === "market")
            return <ResearchMarketTab stage={stagesByType.market ?? null} />;
          if (id === "news")
            return <ResearchNewsTab stage={stagesByType.news ?? null} />;
          if (id === "fundamentals")
            return (
              <ResearchFundamentalsTab
                stage={stagesByType.fundamentals ?? null}
              />
            );
          if (id === "social")
            return <ResearchSocialTab stage={stagesByType.social ?? null} />;
          if (id === "raw") return <ResearchRawTab data={state.data!} />;
          return null;
        }}
      />
    </div>
  );
}

import { useMemo } from "react";
import { NavLink, Outlet, useParams } from "react-router-dom";
import ErrorView from "../../components/ErrorView";
import LoadingView from "../../components/LoadingView";
import { useResearchSession } from "../../hooks/useResearchSession";
import {
  RESEARCH_SESSION_STATUS_LABEL,
  RESEARCH_TAB_LABEL,
} from "../../i18n/ko";
import type { StageAnalysis, StageType } from "../../api/types";
import type { ResearchSessionContext } from "./ResearchSessionContext";
import styles from "./ResearchSessionLayout.module.css";

const STAGE_NAV = [
  { to: "summary", label: RESEARCH_TAB_LABEL.summary },
  { to: "market", label: RESEARCH_TAB_LABEL.market },
  { to: "news", label: RESEARCH_TAB_LABEL.news },
  { to: "fundamentals", label: RESEARCH_TAB_LABEL.fundamentals },
  { to: "social", label: RESEARCH_TAB_LABEL.social },
] as const;

export default function ResearchSessionLayout() {
  const params = useParams<{ sessionId: string }>();
  const sessionId = Number(params.sessionId);
  const validId = Number.isFinite(sessionId) && sessionId > 0;
  const state = useResearchSession(validId ? sessionId : 0);

  const stagesByType = useMemo(() => {
    const map: Partial<Record<StageType, StageAnalysis>> = {};
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
  const sessionStatusKey =
    session.status as keyof typeof RESEARCH_SESSION_STATUS_LABEL;
  const statusLabel =
    RESEARCH_SESSION_STATUS_LABEL[sessionStatusKey] ?? session.status;

  const context: ResearchSessionContext = {
    data: state.data,
    stagesByType,
  };

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

      <nav aria-label="리서치 단계 탐색">
        <ul>
          {STAGE_NAV.map(({ to, label }) => (
            <li key={to}>
              <NavLink to={to} end>
                {label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>

      <Outlet context={context} />
    </div>
  );
}

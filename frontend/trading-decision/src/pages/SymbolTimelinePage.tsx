import { Link, useParams } from "react-router-dom";
import ErrorView from "../components/ErrorView";
import LoadingView from "../components/LoadingView";
import VerdictMiniChart from "../components/VerdictMiniChart";
import { useSymbolTimeline } from "../hooks/useSymbolTimeline";
import {
  RESEARCH_SESSION_STATUS_LABEL,
  STAGE_TYPE_LABEL,
  STAGE_VERDICT_LABEL,
  SUMMARY_DECISION_LABEL,
} from "../i18n/ko";
import styles from "./SymbolTimelinePage.module.css";

export default function SymbolTimelinePage() {
  const params = useParams<{ symbol: string }>();
  const symbol = params.symbol ?? "";
  const state = useSymbolTimeline(symbol, 30);

  if (state.status === "loading" || state.status === "idle")
    return <LoadingView />;
  if (state.status === "not_found")
    return <ErrorView message="해당 종목 데이터가 없습니다." />;
  if (state.status === "error" || !state.data)
    return <ErrorView message={state.error ?? "오류가 발생했습니다."} />;

  return (
    <div className={styles.page}>
      <h1>
        {state.data.symbol} <small>지난 {state.data.days}일</small>
      </h1>

      <section aria-label="평결 차트">
        <VerdictMiniChart entries={state.data.entries} />
      </section>

      <section aria-label="세션 목록">
        <ul>
          {state.data.entries.map((e) => {
            const statusLabel =
              RESEARCH_SESSION_STATUS_LABEL[
                e.status as keyof typeof RESEARCH_SESSION_STATUS_LABEL
              ] ?? e.status;
            return (
              <li key={e.session_id}>
                <Link to={`/research/sessions/${e.session_id}`}>
                  세션 #{e.session_id}
                </Link>{" "}
                · {statusLabel}
                {e.decision ? ` · ${SUMMARY_DECISION_LABEL[e.decision]}` : ""}
                {e.confidence != null ? ` · ${e.confidence}%` : ""}
                <ul>
                  {Object.entries(e.stage_verdicts).map(([stype, verdict]) => (
                    <li key={stype}>
                      {STAGE_TYPE_LABEL[stype as keyof typeof STAGE_TYPE_LABEL]}:{" "}
                      {STAGE_VERDICT_LABEL[
                        verdict as keyof typeof STAGE_VERDICT_LABEL
                      ] ?? verdict}
                    </li>
                  ))}
                </ul>
              </li>
            );
          })}
        </ul>
      </section>
    </div>
  );
}

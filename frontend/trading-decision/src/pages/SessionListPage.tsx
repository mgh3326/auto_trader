import { useState } from "react";
import { Link } from "react-router-dom";
import ErrorView from "../components/ErrorView";
import LoadingView from "../components/LoadingView";
import StatusBadge from "../components/StatusBadge";
import type { SessionStatus } from "../api/types";
import { formatDateTime } from "../format/datetime";
import { useDecisionInbox } from "../hooks/useDecisionInbox";
import {
  COMMON,
  SESSION_STATUS_LABEL,
  WORKFLOW_STATUS_LABEL,
  ACCOUNT_MODE_LABEL,
} from "../i18n";
import { labelOrToken } from "../i18n/formatters";
import styles from "./SessionListPage.module.css";

const pageSize = 50;

export default function SessionListPage() {
  const [offset, setOffset] = useState(0);
  const [statusFilter, setStatusFilter] = useState<SessionStatus | undefined>();
  const inbox = useDecisionInbox({ limit: pageSize, offset, statusFilter });

  return (
    <main className={styles.page}>
      <div className={styles.topbar}>
        <h1>의사결정함</h1>
        <div className={styles.controls}>
          <Link className="btn" to="/news-radar">
            News radar
          </Link>
          <label>
            상태 필터{" "}
            <select
              aria-label="상태 필터"
              onChange={(event) => {
                setOffset(0);
                setStatusFilter(
                  event.target.value === ""
                    ? undefined
                    : (event.target.value as SessionStatus),
                );
              }}
              value={statusFilter ?? ""}
            >
              <option value="">{COMMON.all}</option>
              {(Object.keys(SESSION_STATUS_LABEL) as SessionStatus[]).map((status) => (
                <option key={status} value={status}>
                  {SESSION_STATUS_LABEL[status]}
                </option>
              ))}
            </select>
          </label>
          <button className="btn" onClick={inbox.refetch} type="button">
            {COMMON.refresh}
          </button>
        </div>
      </div>
      {inbox.status === "loading" ? <LoadingView /> : null}
      {inbox.status === "error" ? (
        <ErrorView
          message={inbox.error ?? COMMON.somethingWentWrong}
          onRetry={inbox.refetch}
        />
      ) : null}
      {inbox.status === "success" && inbox.data?.sessions.length === 0 ? (
        <p>아직 의사결정 세션이 없습니다.</p>
      ) : null}
      {inbox.data && inbox.data.sessions.length > 0 ? (
        <>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>생성 시각</th>
                <th>프로필</th>
                <th>전략</th>
                <th>범위</th>
                <th>상태</th>
                <th>워크플로우</th>
                <th>계정</th>
                <th>제안</th>
                <th>대기</th>
              </tr>
            </thead>
            <tbody>
              {inbox.data.sessions.map((session) => (
                <tr key={session.session_uuid}>
                  <td>{formatDateTime(session.generated_at)}</td>
                  <td>{session.source_profile}</td>
                  <td>
                    <Link to={`/sessions/${session.session_uuid}`}>
                      {session.strategy_name ?? session.source_profile}
                    </Link>
                  </td>
                  <td>
                    {session.market_scope
                      ? session.market_scope.toUpperCase()
                      : COMMON.dash}
                  </td>
                  <td>
                    <StatusBadge value={session.status} />
                  </td>
                  <td>
                    <span className={styles.workflowStatusMini}>
                      {labelOrToken(WORKFLOW_STATUS_LABEL, session.workflow_status)}
                    </span>
                  </td>
                  <td>{labelOrToken(ACCOUNT_MODE_LABEL, session.account_mode)}</td>
                  <td>{session.proposals_count}</td>
                  <td>{session.pending_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className={styles.pagination}>
            <button
              className="btn"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - pageSize))}
              type="button"
            >
              {COMMON.previous}
            </button>
            <button
              className="btn"
              disabled={offset + pageSize >= inbox.data.total}
              onClick={() => setOffset(offset + pageSize)}
              type="button"
            >
              {COMMON.next}
            </button>
          </div>
        </>
      ) : null}
    </main>
  );
}

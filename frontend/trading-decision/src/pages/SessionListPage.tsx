import { useState } from "react";
import { Link } from "react-router-dom";
import ErrorView from "../components/ErrorView";
import LoadingView from "../components/LoadingView";
import StatusBadge from "../components/StatusBadge";
import type { SessionStatus } from "../api/types";
import { formatDateTime } from "../format/datetime";
import { useDecisionInbox } from "../hooks/useDecisionInbox";
import styles from "./SessionListPage.module.css";

const pageSize = 50;

export default function SessionListPage() {
  const [offset, setOffset] = useState(0);
  const [statusFilter, setStatusFilter] = useState<SessionStatus | undefined>();
  const inbox = useDecisionInbox({ limit: pageSize, offset, statusFilter });

  return (
    <main className={styles.page}>
      <div className={styles.topbar}>
        <h1>Decision inbox</h1>
        <div className={styles.controls}>
          <label>
            Status filter{" "}
            <select
              aria-label="Status filter"
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
              <option value="">All</option>
              <option value="open">open</option>
              <option value="closed">closed</option>
              <option value="archived">archived</option>
            </select>
          </label>
          <button className="btn" onClick={inbox.refetch} type="button">
            Refresh
          </button>
        </div>
      </div>
      {inbox.status === "loading" ? <LoadingView /> : null}
      {inbox.status === "error" ? (
        <ErrorView
          message={inbox.error ?? "Something went wrong. Try again."}
          onRetry={inbox.refetch}
        />
      ) : null}
      {inbox.status === "success" && inbox.data?.sessions.length === 0 ? (
        <p>No decision sessions yet.</p>
      ) : null}
      {inbox.data && inbox.data.sessions.length > 0 ? (
        <>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Generated</th>
                <th>Profile</th>
                <th>Strategy</th>
                <th>Scope</th>
                <th>Status</th>
                <th>Proposals</th>
                <th>Pending</th>
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
                  <td>{session.market_scope ?? "—"}</td>
                  <td>
                    <StatusBadge value={session.status} />
                  </td>
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
              Previous
            </button>
            <button
              className="btn"
              disabled={offset + pageSize >= inbox.data.total}
              onClick={() => setOffset(offset + pageSize)}
              type="button"
            >
              Next
            </button>
          </div>
        </>
      ) : null}
    </main>
  );
}

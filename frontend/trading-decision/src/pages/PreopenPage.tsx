import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import ErrorView from "../components/ErrorView";
import LoadingView from "../components/LoadingView";
import MarketNewsBriefingSection from "../components/MarketNewsBriefingSection";
import NewsReadinessSection from "../components/NewsReadinessSection";
import { ApiError } from "../api/client";
import {
  createDecisionFromResearchRun,
  getLatestPreopen,
} from "../api/preopen";
import type { PreopenBriefingArtifact, PreopenLatestResponse } from "../api/types";
import { formatDateTime } from "../format/datetime";
import styles from "./PreopenPage.module.css";

type State =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "success"; data: PreopenLatestResponse };

function formatArtifactVersion(version: string): string {
  return version.startsWith("mvp.") ? version.slice(4) : version;
}

function PreopenBriefingArtifactSection({
  artifact,
}: {
  artifact: PreopenBriefingArtifact | null;
}) {
  if (!artifact) return null;

  return (
    <section
      aria-label="Preopen briefing artifact"
      className={styles.artifactSection}
    >
      <div className={styles.artifactHeader}>
        <div>
          <h2>Preopen briefing</h2>
          <p className={styles.meta}>
            {artifact.artifact_type} {formatArtifactVersion(artifact.artifact_version)}
          </p>
        </div>
        <span className={styles.artifactStatus}>Artifact {artifact.status}</span>
      </div>

      {artifact.market_summary ? <p>{artifact.market_summary}</p> : null}
      {artifact.news_summary ? <p>News brief: {artifact.news_summary}</p> : null}

      {artifact.risk_notes.length > 0 ? (
        <ul aria-label="Preopen artifact risk notes" className={styles.warnings}>
          {artifact.risk_notes.map((note) => (
            <li className={styles.warningChip} key={note}>
              {note}
            </li>
          ))}
        </ul>
      ) : null}

      {artifact.readiness.length > 0 ? (
        <div className={styles.artifactGrid}>
          {artifact.readiness.map((item) => (
            <div className={styles.artifactCard} key={item.key}>
              <strong>{item.key}</strong>
              <span>{item.status}</span>
            </div>
          ))}
        </div>
      ) : null}

      {artifact.sections.length > 0 ? (
        <div className={styles.artifactGrid}>
          {artifact.sections.map((section) => (
            <div className={styles.artifactCard} key={section.section_id}>
              <strong>{section.title}</strong>
              <span>
                {section.status} · {section.item_count}
              </span>
              {section.summary ? <small>{section.summary}</small> : null}
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

export default function PreopenPage() {
  const navigate = useNavigate();
  const [state, setState] = useState<State>({ status: "loading" });
  const [version, setVersion] = useState(0);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [confirmPending, setConfirmPending] = useState(false);

  const refetch = useCallback(() => setVersion((v) => v + 1), []);

  useEffect(() => {
    setState({ status: "loading" });
    const controller = new AbortController();
    getLatestPreopen("kr")
      .then((data) => {
        if (!controller.signal.aborted) {
          setState({ status: "success", data });
        }
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        if (err instanceof ApiError && err.status === 401) {
          window.location.assign(
            `/login?next=${encodeURIComponent(window.location.pathname)}`,
          );
          return;
        }
        setState({
          status: "error",
          message:
            err instanceof ApiError ? err.detail : "Something went wrong. Try again.",
        });
      });
    return () => controller.abort();
  }, [version]);

  const handleCreate = useCallback(async (runUuid: string) => {
    if (confirmPending) {
      setConfirmPending(false);
      setCreating(true);
      setCreateError(null);
      try {
        const result = await createDecisionFromResearchRun({ runUuid });
        navigate(`/sessions/${result.session_uuid}`);
      } catch (err: unknown) {
        setCreateError(
          err instanceof ApiError
            ? err.detail
            : "Failed to create decision session.",
        );
      } finally {
        setCreating(false);
      }
    } else {
      setConfirmPending(true);
    }
  }, [confirmPending, navigate]);

  if (state.status === "loading") return <LoadingView />;
  if (state.status === "error") {
    return (
      <main className={styles.page}>
        <ErrorView message={state.message} onRetry={refetch} />
      </main>
    );
  }

  const { data } = state;
  const artifactCta = data.briefing_artifact?.cta ?? null;
  const linkedSessionUuid =
    artifactCta?.state === "linked_session_exists"
      ? artifactCta.linked_session_uuid
      : data.linked_sessions[0]?.session_uuid;
  const createRunUuid =
    artifactCta?.state === "create_available"
      ? artifactCta.run_uuid
      : data.run_uuid;

  if (!data.has_run) {
    return (
      <main className={styles.page}>
        <h1>Preopen advisory</h1>
        <div className={styles.banner} role="status">
          <strong>No preopen research run available</strong>
          {data.advisory_skipped_reason ? (
            <p>Reason: {data.advisory_skipped_reason}</p>
          ) : null}
        </div>
        <PreopenBriefingArtifactSection artifact={data.briefing_artifact} />
      </main>
    );
  }

  return (
    <main className={styles.page}>
      <div className={styles.header}>
        <h1>Preopen advisory</h1>
        <div className={styles.meta}>
          Generated: {formatDateTime(data.generated_at)}
          {data.strategy_name ? ` · ${data.strategy_name}` : ""}
          {data.source_profile ? ` · ${data.source_profile}` : ""}
          {data.market_scope ? ` · ${data.market_scope.toUpperCase()}` : ""}
          {` · Advisory ${data.advisory_used ? "used" : "not used"}`}
        </div>
        {data.market_brief && typeof data.market_brief.summary === "string" ? (
          <p>{data.market_brief.summary}</p>
        ) : null}
      </div>

      {data.advisory_skipped_reason ? (
        <div className={styles.banner} role="status">
          Advisory notice: {data.advisory_skipped_reason}
        </div>
      ) : null}

      {data.source_warnings.length > 0 ? (
        <ul aria-label="Source warnings" className={styles.warnings}>
          {data.source_warnings.map((w, i) => (
            <li className={styles.warningChip} key={i}>
              {w}
            </li>
          ))}
        </ul>
      ) : null}

      <PreopenBriefingArtifactSection artifact={data.briefing_artifact} />
      <NewsReadinessSection news={data.news} preview={data.news_preview} />
      <MarketNewsBriefingSection briefing={data.market_news_briefing} />

      {data.candidates.length > 0 ? (
        <section className={styles.section}>
          <h2>Candidates ({data.candidate_count})</h2>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Side</th>
                <th>Kind</th>
                <th>Confidence</th>
                <th>Price</th>
                <th>Qty</th>
                <th>Rationale</th>
              </tr>
            </thead>
            <tbody>
              {data.candidates.map((c) => (
                <tr key={c.candidate_uuid}>
                  <td>{c.symbol}</td>
                  <td>
                    <span
                      className={
                        c.side === "buy"
                          ? styles.sideBuy
                          : c.side === "sell"
                            ? styles.sideSell
                            : styles.sideNone
                      }
                    >
                      {c.side}
                    </span>
                  </td>
                  <td>{c.candidate_kind}</td>
                  <td>{c.confidence !== null ? `${c.confidence}%` : "—"}</td>
                  <td>
                    {c.proposed_price !== null
                      ? `${c.proposed_price} ${c.currency ?? ""}`
                      : "—"}
                  </td>
                  <td>{c.proposed_qty !== null ? c.proposed_qty : "—"}</td>
                  <td className={styles.rationale} title={c.rationale ?? ""}>
                    {c.rationale ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}

      {data.reconciliations.length > 0 ? (
        <section className={styles.section}>
          <h2>Pending reconciliations ({data.reconciliation_count})</h2>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Classification</th>
                <th>NXT class</th>
                <th>Actionable</th>
                <th>Gap %</th>
                <th>Summary</th>
              </tr>
            </thead>
            <tbody>
              {data.reconciliations.map((r, i) => (
                <tr key={i}>
                  <td>{r.symbol}</td>
                  <td>{r.classification}</td>
                  <td>{r.nxt_classification ?? "—"}</td>
                  <td>
                    {r.nxt_actionable === null
                      ? "—"
                      : r.nxt_actionable
                        ? "Yes"
                        : "No"}
                  </td>
                  <td>{r.gap_pct !== null ? `${r.gap_pct}%` : "—"}</td>
                  <td>{r.summary ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}

      {data.linked_sessions.length > 0 ? (
        <section className={styles.section}>
          <h2>Linked decision sessions</h2>
          <ul className={styles.linkedSessions}>
            {data.linked_sessions.map((s) => (
              <li className={styles.linkedSessionItem} key={String(s.session_uuid)}>
                <Link to={`/sessions/${s.session_uuid}`}>
                  {String(s.session_uuid)}
                </Link>
                <span>{s.status}</span>
                <span>{formatDateTime(s.created_at)}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <div className={styles.ctaRow}>
        {linkedSessionUuid ? (
          <Link
            className="btn"
            to={`/sessions/${linkedSessionUuid}`}
          >
            Open session
          </Link>
        ) : null}
        {!linkedSessionUuid ? (
          <button
            className="btn"
            disabled={creating || !createRunUuid || artifactCta?.state === "unavailable"}
            onClick={() => createRunUuid && handleCreate(String(createRunUuid))}
            type="button"
          >
            {confirmPending
              ? "Confirm create decision session?"
              : creating
                ? "Creating…"
                : artifactCta?.label ?? "Create decision session"}
          </button>
        ) : null}
        {confirmPending ? (
          <button
            className="btn"
            onClick={() => setConfirmPending(false)}
            type="button"
          >
            Cancel
          </button>
        ) : null}
        {createError ? (
          <span className={styles.inlineError} role="alert">
            {createError}
          </span>
        ) : null}
      </div>

      {data.notes ? <p>{data.notes}</p> : null}
    </main>
  );
}

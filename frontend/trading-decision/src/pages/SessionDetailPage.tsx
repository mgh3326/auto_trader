import { Link, useParams } from "react-router-dom";
import AnalyticsMatrix from "../components/AnalyticsMatrix";
import { CommitteeEvidenceArtifacts } from "../components/CommitteeEvidenceArtifacts";
import { CommitteeExecutionPreview } from "../components/CommitteeExecutionPreview";
import { CommitteePortfolioApproval } from "../components/CommitteePortfolioApproval";
import { CommitteeRiskReview } from "../components/CommitteeRiskReview";
import { CommitteeWorkflowTransition } from "../components/CommitteeWorkflowTransition";
import ErrorView from "../components/ErrorView";
import LoadingView from "../components/LoadingView";
import MarketBriefPanel from "../components/MarketBriefPanel";
import OperatorEventForm from "../components/OperatorEventForm";
import ProposalRow from "../components/ProposalRow";
import StatusBadge from "../components/StatusBadge";
import StrategyEventTimeline from "../components/StrategyEventTimeline";
import { formatDateTime } from "../format/datetime";
import { useCommitteeWorkflow } from "../hooks/useCommitteeWorkflow";
import { useDecisionSession } from "../hooks/useDecisionSession";
import { useSessionAnalytics } from "../hooks/useSessionAnalytics";
import { useStrategyEvents } from "../hooks/useStrategyEvents";
import styles from "./SessionDetailPage.module.css";

export default function SessionDetailPage() {
  const { sessionUuid } = useParams();
  const session = useDecisionSession(sessionUuid ?? "");
  const analytics = useSessionAnalytics(sessionUuid ?? "");
  const strategyEvents = useStrategyEvents(sessionUuid ?? "");

  const committeeWorkflow = useCommitteeWorkflow(
    session.data as any,
    (_updated) => {
      // Synchronize back to useDecisionSession cache if needed, 
      // but useCommitteeWorkflow handles its own state for now.
    }
  );

  if (!sessionUuid) {
    return <ErrorView message="Session not found" />;
  }
  if (session.status === "loading" || session.status === "idle") {
    return <LoadingView />;
  }
  if (session.status === "not_found") {
    return (
      <main className={styles.page}>
        <h1>Session not found</h1>
        <Link className="btn" to="/">
          Back to inbox
        </Link>
      </main>
    );
  }
  if (session.status === "error" || !session.data) {
    return (
      <ErrorView
        message={session.error ?? "Something went wrong. Try again."}
        onRetry={session.refetch}
      />
    );
  }

  const data = committeeWorkflow.session || session.data;
  const isCommitteeSession = data.source_profile === "committee_mock_paper";

  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <Link to="/">Back to inbox</Link>
        <div className={styles.titleRow}>
          <h1>{data.strategy_name ?? data.source_profile}</h1>
          <StatusBadge value={data.status} />
        </div>
        <p>
          {data.source_profile} · {data.market_scope ?? "all markets"} ·{" "}
          {formatDateTime(data.generated_at)}
        </p>
        {isCommitteeSession && data.workflow_status && (
          <div className={styles.workflowRow}>
            <strong>Workflow Status:</strong> <span className={styles.workflowStatus}>{data.workflow_status.replace(/_/g, " ").toUpperCase()}</span>
          </div>
        )}
      </header>
      <MarketBriefPanel brief={data.market_brief} notes={data.notes} />

      {isCommitteeSession && data.artifacts && (
        <section className={styles.committeeArtifacts} aria-label="Committee artifacts">
          <CommitteeEvidenceArtifacts artifacts={data.artifacts} />
          <CommitteeRiskReview riskReview={data.artifacts.risk_review ?? null} />
          <CommitteePortfolioApproval portfolioApproval={data.artifacts.portfolio_approval ?? null} />
          <CommitteeExecutionPreview executionPreview={data.artifacts.execution_preview ?? null} />
        </section>
      )}

      {isCommitteeSession && (
        <CommitteeWorkflowTransition
          currentStatus={data.workflow_status ?? null}
          isUpdating={committeeWorkflow.isUpdating}
          onTransition={committeeWorkflow.transitionTo}
        />
      )}

      {analytics.status === "loading" ? (
        <section className={styles.analytics} aria-label="Analytics">
          <h2>Outcome analytics</h2>
          <p>Loading analytics...</p>
        </section>
      ) : null}
      {analytics.status === "error" ? (
        <section className={styles.analytics} aria-label="Analytics">
          <h2>Outcome analytics</h2>
          <p role="alert">{analytics.error}</p>
        </section>
      ) : null}
      {analytics.status === "success" && analytics.data ? (
        <section className={styles.analytics} aria-label="Analytics">
          <h2>Outcome analytics</h2>
          <AnalyticsMatrix data={analytics.data} />
        </section>
      ) : null}
      <section
        className={styles.strategyEvents}
        aria-label="Strategy events"
      >
        <h2>Strategy events</h2>
        <OperatorEventForm
          sessionUuid={data.session_uuid}
          onSubmit={(body) => strategyEvents.submit(body)}
        />
        {strategyEvents.status === "loading" ||
        strategyEvents.status === "idle" ? (
          <p>Loading strategy events...</p>
        ) : null}
        {strategyEvents.status === "error" ? (
          <p role="alert">{strategyEvents.error}</p>
        ) : null}
        {strategyEvents.status === "not_found" ? (
          <p role="alert">Session not found for strategy events.</p>
        ) : null}
        {strategyEvents.status === "success" && strategyEvents.data ? (
          <StrategyEventTimeline events={strategyEvents.data.events} />
        ) : null}
      </section>
      <section className={styles.proposals} aria-label="Proposals">
        {data.proposals.map((proposal) => (
          <ProposalRow
            key={proposal.proposal_uuid}
            onRecordOutcome={session.recordOutcome}
            onRespond={session.respond}
            proposal={proposal}
          />
        ))}
      </section>
      <footer className={styles.footer}>
        {data.pending_count} of {data.proposals_count} pending
      </footer>
    </main>
  );
}

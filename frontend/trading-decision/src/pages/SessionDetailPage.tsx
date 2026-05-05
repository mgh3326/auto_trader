import { Link, useParams } from "react-router-dom";
import AnalyticsMatrix from "../components/AnalyticsMatrix";
import { CommitteeEvidenceArtifacts } from "../components/CommitteeEvidenceArtifacts";
import { CommitteeExecutionPreview } from "../components/CommitteeExecutionPreview";
import { CommitteeJournalPlaceholder } from "../components/CommitteeJournalPlaceholder";
import { CommitteePortfolioApproval } from "../components/CommitteePortfolioApproval";
import { CommitteeResearchDebate } from "../components/CommitteeResearchDebate";
import { CommitteeRiskReview } from "../components/CommitteeRiskReview";
import { CommitteeTraderDraft } from "../components/CommitteeTraderDraft";
import { CommitteeWorkflowTransition } from "../components/CommitteeWorkflowTransition";
import ErrorView from "../components/ErrorView";
import LoadingView from "../components/LoadingView";
import MarketBriefPanel from "../components/MarketBriefPanel";
import OperatorEventForm from "../components/OperatorEventForm";
import ProposalRow from "../components/ProposalRow";
import StatusBadge from "../components/StatusBadge";
import StrategyEventTimeline from "../components/StrategyEventTimeline";
import type { SessionDetail } from "../api/types";
import { formatDateTime } from "../format/datetime";
import { useCommitteeWorkflow } from "../hooks/useCommitteeWorkflow";
import { useDecisionSession } from "../hooks/useDecisionSession";
import { useSessionAnalytics } from "../hooks/useSessionAnalytics";
import { useStrategyEvents } from "../hooks/useStrategyEvents";
import { COMMON, WORKFLOW_STATUS_LABEL } from "../i18n";
import { labelOrToken } from "../i18n/formatters";
import styles from "./SessionDetailPage.module.css";

export default function SessionDetailPage() {
  const { sessionUuid } = useParams();
  const session = useDecisionSession(sessionUuid ?? "");
  const analytics = useSessionAnalytics(sessionUuid ?? "");
  const strategyEvents = useStrategyEvents(sessionUuid ?? "");

  const committeeWorkflow = useCommitteeWorkflow(
    session.data as SessionDetail | null,
    (_updated) => {
      // Synchronize back to useDecisionSession cache if needed,
      // but useCommitteeWorkflow handles its own state for now.
    },
  );

  if (!sessionUuid) {
    return <ErrorView message="세션을 찾을 수 없습니다" />;
  }
  if (session.status === "loading" || session.status === "idle") {
    return <LoadingView />;
  }
  if (session.status === "not_found") {
    return (
      <main className={styles.page}>
        <h1>세션을 찾을 수 없습니다</h1>
        <Link className="btn" to="/">
          의사결정함으로 돌아가기
        </Link>
      </main>
    );
  }
  if (session.status === "error" || !session.data) {
    return (
      <ErrorView
        message={session.error ?? COMMON.somethingWentWrong}
        onRetry={session.refetch}
      />
    );
  }

  const data = committeeWorkflow.session || session.data;
  const isCommitteeSession = data.source_profile === "committee_mock_paper";

  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <Link to="/">의사결정함으로 돌아가기</Link>
        <div className={styles.titleRow}>
          <h1>{data.strategy_name ?? data.source_profile}</h1>
          <StatusBadge value={data.status} />
        </div>
        <p>
          {data.source_profile} · {data.market_scope ?? "전체 시장"} ·{" "}
          {formatDateTime(data.generated_at)}
        </p>
        {isCommitteeSession && data.workflow_status && (
          <div className={styles.workflowRow}>
            <strong>워크플로우 상태:</strong>{" "}
            <span className={styles.workflowStatus}>
              {labelOrToken(WORKFLOW_STATUS_LABEL, data.workflow_status)}
            </span>
          </div>
        )}
      </header>
      <MarketBriefPanel brief={data.market_brief} notes={data.notes} />

      {isCommitteeSession && data.artifacts && (
        <section className={styles.committeeArtifacts} aria-label="위원회 산출물">
          <CommitteeEvidenceArtifacts artifacts={data.artifacts} />
          <CommitteeResearchDebate
            researchDebate={data.artifacts.research_debate ?? null}
          />
          <CommitteeTraderDraft
            traderDraft={data.artifacts.trader_draft ?? null}
          />
          <CommitteeRiskReview riskReview={data.artifacts.risk_review ?? null} />
          <CommitteePortfolioApproval portfolioApproval={data.artifacts.portfolio_approval ?? null} />
          <CommitteeExecutionPreview executionPreview={data.artifacts.execution_preview ?? null} />
          <CommitteeJournalPlaceholder
            journalPlaceholder={data.artifacts.journal_placeholder ?? null}
          />
        </section>
      )}

      {isCommitteeSession && committeeWorkflow.error ? (
        <p className={styles.committeeError} role="alert">
          {committeeWorkflow.error.message}
        </p>
      ) : null}

      {isCommitteeSession && (
        <CommitteeWorkflowTransition
          currentStatus={data.workflow_status ?? null}
          accountMode={data.account_mode ?? null}
          isUpdating={committeeWorkflow.isUpdating}
          onTransition={committeeWorkflow.transitionTo}
        />
      )}

      {analytics.status === "loading" ? (
        <section className={styles.analytics} aria-label="분석">
          <h2>결과 분석</h2>
          <p>분석을 불러오는 중...</p>
        </section>
      ) : null}
      {analytics.status === "error" ? (
        <section className={styles.analytics} aria-label="분석">
          <h2>결과 분석</h2>
          <p role="alert">{analytics.error}</p>
        </section>
      ) : null}
      {analytics.status === "success" && analytics.data ? (
        <section className={styles.analytics} aria-label="분석">
          <h2>결과 분석</h2>
          <AnalyticsMatrix data={analytics.data} />
        </section>
      ) : null}
      <section
        className={styles.strategyEvents}
        aria-label="전략 이벤트"
      >
        <h2>전략 이벤트</h2>
        <OperatorEventForm
          sessionUuid={data.session_uuid}
          onSubmit={(body) => strategyEvents.submit(body)}
        />
        {strategyEvents.status === "loading" ||
        strategyEvents.status === "idle" ? (
          <p>전략 이벤트를 불러오는 중...</p>
        ) : null}
        {strategyEvents.status === "error" ? (
          <p role="alert">{strategyEvents.error}</p>
        ) : null}
        {strategyEvents.status === "not_found" ? (
          <p role="alert">전략 이벤트용 세션을 찾을 수 없습니다.</p>
        ) : null}
        {strategyEvents.status === "success" && strategyEvents.data ? (
          <StrategyEventTimeline events={strategyEvents.data.events} />
        ) : null}
      </section>
      <section className={styles.proposals} aria-label="제안">
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
        {data.pending_count}/{data.proposals_count} 대기 중
      </footer>
    </main>
  );
}

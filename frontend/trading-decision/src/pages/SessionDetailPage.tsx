import { Link, useParams } from "react-router-dom";
import ErrorView from "../components/ErrorView";
import LoadingView from "../components/LoadingView";
import MarketBriefPanel from "../components/MarketBriefPanel";
import ProposalRow from "../components/ProposalRow";
import StatusBadge from "../components/StatusBadge";
import { formatDateTime } from "../format/datetime";
import { useDecisionSession } from "../hooks/useDecisionSession";
import styles from "./SessionDetailPage.module.css";

export default function SessionDetailPage() {
  const { sessionUuid } = useParams();
  const session = useDecisionSession(sessionUuid ?? "");

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

  const data = session.data;
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
      </header>
      <MarketBriefPanel brief={data.market_brief} notes={data.notes} />
      <section className={styles.proposals} aria-label="Proposals">
        {data.proposals.map((proposal) => (
          <ProposalRow
            key={proposal.proposal_uuid}
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

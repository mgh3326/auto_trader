import { useEffect, useState } from "react";

import {
  getRetrospectiveOverview,
  getRetrospectiveStagePerformance,
  listRetrospectiveDecisions,
} from "../api/researchRetrospective";
import type {
  Market,
  RetrospectiveDecisionsResponse,
  RetrospectiveOverview,
  RetrospectiveStagePerformanceRow,
} from "../api/types";
import { retrospective, COMMON } from "../i18n";
import styles from "./RetrospectivePage.module.css";

type MarketFilter = Market | "ALL";

const DAYS_OPTIONS = [7, 14, 30, 60, 90];

function fmtPct(v: number | null, digits = 2): string {
  return v === null || Number.isNaN(v) ? "—" : `${v.toFixed(digits)}%`;
}

export default function RetrospectivePage() {
  const [days, setDays] = useState(30);
  const [marketFilter, setMarketFilter] = useState<MarketFilter>("ALL");
  const [overview, setOverview] = useState<RetrospectiveOverview | null>(null);
  const [stage, setStage] = useState<RetrospectiveStagePerformanceRow[]>([]);
  const [decisions, setDecisions] =
    useState<RetrospectiveDecisionsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const filters = {
      days,
      market: marketFilter === "ALL" ? undefined : marketFilter,
    };
    Promise.all([
      getRetrospectiveOverview(filters),
      getRetrospectiveStagePerformance(filters),
      listRetrospectiveDecisions({ ...filters, limit: 20 }),
    ])
      .then(([ov, stg, dec]) => {
        if (cancelled) return;
        setOverview(ov);
        setStage(stg);
        setDecisions(dec);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        console.error(err);
        setError(retrospective.loadError);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [days, marketFilter]);

  if (loading) return <div className={styles.loading}>{COMMON.loading}</div>;
  if (error) return <div className={styles.error}>{error}</div>;
  if (overview === null)
    return <div className={styles.error}>{retrospective.loadError}</div>;

  const showWarning = overview.warnings.includes(
    "no_research_summaries_in_window",
  );

  return (
    <div className={styles.container}>
      <header className={styles.header}>
        <div>
          <h1>{retrospective.pageTitle}</h1>
          <p>{retrospective.pageSubtitle}</p>
        </div>
        <div className={styles.filters}>
          <label htmlFor="retrospective-days">
            {retrospective.filterDays}
          </label>
          <select
            id="retrospective-days"
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
          >
            {DAYS_OPTIONS.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
          <label htmlFor="retrospective-market">
            {retrospective.filterMarket}
          </label>
          <select
            id="retrospective-market"
            value={marketFilter}
            onChange={(e) => setMarketFilter(e.target.value as MarketFilter)}
          >
            <option value="ALL">{retrospective.filterAll}</option>
            <option value="KR">{retrospective.marketKR}</option>
            <option value="US">{retrospective.marketUS}</option>
            <option value="CRYPTO">{retrospective.marketCRYPTO}</option>
          </select>
        </div>
      </header>

      {showWarning ? (
        <div className={styles.warning}>{retrospective.warningEmpty}</div>
      ) : null}

      <section className={styles.cards}>
        <div className={styles.card}>
          <h3>{retrospective.cards.sessions}</h3>
          <p>{overview.sessions_total}</p>
        </div>
        <div className={styles.card}>
          <h3>{retrospective.cards.summaries}</h3>
          <p>{overview.summaries_total}</p>
        </div>
        <div className={styles.card}>
          <h3>{retrospective.cards.realizedPnl}</h3>
          <p>{fmtPct(overview.pnl.realized_pnl_pct_avg)}</p>
        </div>
        <div className={styles.card}>
          <h3>{retrospective.cards.unrealizedPnl}</h3>
          <p>{fmtPct(overview.pnl.unrealized_pnl_pct_avg)}</p>
        </div>
      </section>

      <section className={styles.section}>
        <h2>{retrospective.distribution.title}</h2>
        <ul className={styles.kv}>
          <li>
            {retrospective.distribution.aiBuy}: {overview.decision_distribution.ai_buy}
          </li>
          <li>
            {retrospective.distribution.aiHold}: {overview.decision_distribution.ai_hold}
          </li>
          <li>
            {retrospective.distribution.aiSell}: {overview.decision_distribution.ai_sell}
          </li>
          <li>
            {retrospective.distribution.userAccept}:{" "}
            {overview.decision_distribution.user_accept}
          </li>
          <li>
            {retrospective.distribution.userReject}:{" "}
            {overview.decision_distribution.user_reject}
          </li>
          <li>
            {retrospective.distribution.userModify}:{" "}
            {overview.decision_distribution.user_modify}
          </li>
          <li>
            {retrospective.distribution.userDefer}:{" "}
            {overview.decision_distribution.user_defer}
          </li>
          <li>
            {retrospective.distribution.userPending}:{" "}
            {overview.decision_distribution.user_pending}
          </li>
        </ul>
      </section>

      <section className={styles.section}>
        <h2>{retrospective.stageCoverage.title}</h2>
        <table className={styles.table}>
          <thead>
            <tr>
              <th>{retrospective.stageCoverage.stage}</th>
              <th>{retrospective.stageCoverage.coverage}</th>
              <th>{retrospective.stageCoverage.stale}</th>
              <th>{retrospective.stageCoverage.unavailable}</th>
            </tr>
          </thead>
          <tbody>
            {overview.stage_coverage.map((s) => (
              <tr key={s.stage_type}>
                <td>{s.stage_type}</td>
                <td>{s.coverage_pct.toFixed(1)}%</td>
                <td>{s.stale_pct.toFixed(1)}%</td>
                <td>{s.unavailable_pct.toFixed(1)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className={styles.section}>
        <h2>{retrospective.stagePerformance.title}</h2>
        {stage.length === 0 ? (
          <p className={styles.empty}>{retrospective.empty}</p>
        ) : (
          <table className={styles.table}>
            <thead>
              <tr>
                <th>{retrospective.stagePerformance.combo}</th>
                <th>{retrospective.stagePerformance.sample}</th>
                <th>{retrospective.stagePerformance.winRate}</th>
                <th>{retrospective.stagePerformance.avgPnl}</th>
              </tr>
            </thead>
            <tbody>
              {stage.map((row) => (
                <tr key={row.stage_combo}>
                  <td>{row.stage_combo}</td>
                  <td>{row.sample_size}</td>
                  <td>{fmtPct(row.win_rate_pct, 1)}</td>
                  <td>{fmtPct(row.avg_realized_pnl_pct, 2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className={styles.section}>
        <h2>{retrospective.decisions.title}</h2>
        {decisions === null || decisions.rows.length === 0 ? (
          <p className={styles.empty}>{retrospective.empty}</p>
        ) : (
          <table className={styles.table}>
            <thead>
              <tr>
                <th>{retrospective.decisions.symbol}</th>
                <th>{retrospective.decisions.market}</th>
                <th>{retrospective.decisions.decidedAt}</th>
                <th>{retrospective.decisions.ai}</th>
                <th>{retrospective.decisions.user}</th>
                <th>{retrospective.decisions.realized}</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {decisions.rows.map((row) => (
                <tr
                  key={`${row.research_session_id}-${row.proposal_id ?? "noprop"}`}
                >
                  <td>{row.symbol}</td>
                  <td>{row.market}</td>
                  <td>{new Date(row.decided_at).toLocaleString()}</td>
                  <td>{row.ai_decision ?? "—"}</td>
                  <td>{row.user_response ?? "—"}</td>
                  <td>{fmtPct(row.realized_pnl_pct, 2)}</td>
                  <td>
                    <a
                      className={styles.link}
                      href={`/trading/decisions/research/sessions/${row.research_session_id}/summary`}
                    >
                      {retrospective.decisions.open}
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

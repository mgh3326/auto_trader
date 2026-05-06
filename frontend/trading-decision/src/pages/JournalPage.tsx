import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import {
  createJournal,
  getJournalCoverage,
  updateJournal,
} from "../api/tradeJournals";
import type {
  JournalCoverageResponse,
  JournalCoverageRow,
  JournalCreateRequest,
  JournalUpdateRequest,
  Market,
} from "../api/types";
import {
  JournalModal,
  type JournalModalSubmitPayload,
} from "../components/JournalModal";
import { tradeJournal, COMMON } from "../i18n";
import styles from "./JournalPage.module.css";

type MarketFilter = Market | "ALL";

const MARKETS: MarketFilter[] = ["ALL", "KR", "US", "CRYPTO"];

function formatNumber(n: number | null, digits = 2): string {
  return n === null || Number.isNaN(n) ? "—" : n.toFixed(digits);
}

function statusLabel(status: JournalCoverageRow["journal_status"]): string {
  switch (status) {
    case "present":
      return tradeJournal.statusPresent;
    case "missing":
      return tradeJournal.statusMissing;
    case "stale":
      return tradeJournal.statusStale;
  }
}

export default function JournalPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const marketParam = searchParams.get("market");
  const marketFilter: MarketFilter = (
    (MARKETS as string[]).includes(marketParam ?? "ALL") ? marketParam : "ALL"
  ) as MarketFilter;

  const [data, setData] = useState<JournalCoverageResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedRow, setSelectedRow] = useState<JournalCoverageRow | null>(null);
  const [modalOpen, setModalOpen] = useState(false);

  const fetchData = useCallback(() => {
    setLoading(true);
    setError(null);
    getJournalCoverage(marketFilter === "ALL" ? undefined : marketFilter)
      .then((res) => setData(res))
      .catch((err: unknown) => {
        console.error(err);
        setError(tradeJournal.loadError);
      })
      .finally(() => setLoading(false));
  }, [marketFilter]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const handleSave = async (payload: JournalModalSubmitPayload) => {
    if (selectedRow === null) return;
    if (selectedRow.journal_id !== null) {
      await updateJournal(selectedRow.journal_id, payload as JournalUpdateRequest);
    } else {
      await createJournal(payload as JournalCreateRequest);
    }
    fetchData();
  };

  if (loading) return <div className={styles.loading}>{COMMON.loading}</div>;
  if (error) return <div className={styles.error}>{error}</div>;
  if (data === null) return <div className={styles.error}>{tradeJournal.loadError}</div>;

  return (
    <div className={styles.container}>
      <header className={styles.header}>
        <div>
          <h1>{tradeJournal.pageTitle}</h1>
          <p>{tradeJournal.pageSubtitle}</p>
        </div>
        <div className={styles.filters}>
          <label htmlFor="journal-market-filter">
            {tradeJournal.filterMarket}
          </label>
          <select
            id="journal-market-filter"
            value={marketFilter}
            onChange={(e) => setSearchParams({ market: e.target.value })}
          >
            <option value="ALL">{tradeJournal.marketAll}</option>
            <option value="KR">{tradeJournal.marketKR}</option>
            <option value="US">{tradeJournal.marketUS}</option>
            <option value="CRYPTO">{tradeJournal.marketCRYPTO}</option>
          </select>
        </div>
      </header>

      <table className={styles.table}>
        <thead>
          <tr>
            <th>{tradeJournal.colSymbol}</th>
            <th>{tradeJournal.colWeight}</th>
            <th>{tradeJournal.colStatus}</th>
            <th>{tradeJournal.colThesis}</th>
            <th>{tradeJournal.colTarget}</th>
            <th>{tradeJournal.colStop}</th>
            <th>{tradeJournal.colMinHold}</th>
            <th>{tradeJournal.colResearch}</th>
            <th>{tradeJournal.colActions}</th>
          </tr>
        </thead>
        <tbody>
          {data.rows.map((row) => (
            <tr
              key={row.symbol}
              className={
                row.thesis_conflict_with_summary ? styles.conflictRow : undefined
              }
            >
              <td>
                <div className={styles.symbolCell}>
                  <span className={styles.symbol}>{row.symbol}</span>
                  {row.name ? (
                    <span className={styles.name}>{row.name}</span>
                  ) : null}
                </div>
              </td>
              <td>{formatNumber(row.position_weight_pct, 1)}%</td>
              <td>
                <span
                  className={`${styles.statusBadge} ${
                    styles[`status_${row.journal_status}`] ?? ""
                  }`}
                >
                  {statusLabel(row.journal_status)}
                </span>
                {row.thesis_conflict_with_summary ? (
                  <span className={styles.conflictBadge}>
                    {tradeJournal.conflictWarning}
                  </span>
                ) : null}
              </td>
              <td className={styles.thesisCell}>{row.thesis ?? "—"}</td>
              <td>{formatNumber(row.target_price)}</td>
              <td>{formatNumber(row.stop_loss)}</td>
              <td>{row.min_hold_days !== null ? `${row.min_hold_days}d` : "—"}</td>
              <td>
                {row.latest_summary_decision ? (
                  <span
                    className={`${styles.decisionBadge} ${
                      styles[`decision_${row.latest_summary_decision}`] ?? ""
                    }`}
                  >
                    {row.latest_summary_decision.toUpperCase()}
                  </span>
                ) : (
                  "—"
                )}
              </td>
              <td>
                <button
                  type="button"
                  onClick={() => {
                    setSelectedRow(row);
                    setModalOpen(true);
                  }}
                >
                  {row.journal_id !== null
                    ? tradeJournal.actionEdit
                    : tradeJournal.actionCreate}
                </button>
                {row.latest_research_session_id !== null ? (
                  <a
                    className={styles.researchLink}
                    href={`/trading/decisions/research/sessions/${row.latest_research_session_id}/summary`}
                  >
                    {tradeJournal.actionResearch}
                  </a>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {data.total === 0 ? (
        <div className={styles.empty}>{tradeJournal.empty}</div>
      ) : null}

      {modalOpen && selectedRow !== null ? (
        <JournalModal
          isOpen={modalOpen}
          mode={selectedRow.journal_id !== null ? "edit" : "create"}
          symbol={selectedRow.symbol}
          instrumentType={selectedRow.instrument_type ?? "equity_kr"}
          initialRow={selectedRow}
          onClose={() => setModalOpen(false)}
          onSave={handleSave}
        />
      ) : null}
    </div>
  );
}

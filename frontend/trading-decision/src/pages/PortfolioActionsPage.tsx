// frontend/trading-decision/src/pages/PortfolioActionsPage.tsx
import { useEffect, useMemo, useState } from "react";

import PortfolioActionRow from "../components/PortfolioActionRow";
import { getPortfolioActions } from "../api/portfolioActions";
import type {
  CandidateAction,
  Market,
  PortfolioActionCandidate,
  PortfolioActionsResponse,
} from "../api/types";
import { portfolioActions as t } from "../i18n/ko";
import styles from "./PortfolioActionsPage.module.css";

const ACTIONS: (CandidateAction | "ALL")[] = [
  "ALL", "sell", "trim", "hold", "add", "watch",
];

const ACTION_LABEL_KEY: Record<CandidateAction, keyof typeof t> = {
  sell: "actionSell",
  trim: "actionTrim",
  hold: "actionHold",
  add: "actionAdd",
  watch: "actionWatch",
};

export default function PortfolioActionsPage() {
  const [data, setData] = useState<PortfolioActionsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [market, setMarket] = useState<Market | "ALL">("ALL");
  const [actionFilter, setActionFilter] = useState<CandidateAction | "ALL">("ALL");

  useEffect(() => {
    let cancelled = false;
    setError(null);
    getPortfolioActions(market === "ALL" ? undefined : market)
      .then((res) => {
        if (!cancelled) setData(res);
      })
      .catch(() => {
        if (!cancelled) setError(t.loadError);
      });
    return () => {
      cancelled = true;
    };
  }, [market]);

  const filtered = useMemo<PortfolioActionCandidate[]>(() => {
    if (!data) return [];
    if (actionFilter === "ALL") return data.candidates;
    return data.candidates.filter((c) => c.candidate_action === actionFilter);
  }, [data, actionFilter]);

  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <h1>{t.pageTitle}</h1>
        <p>{t.pageSubtitle}</p>
      </header>

      <div className={styles.filters}>
        <label>
          {t.filterMarket}
          <select
            value={market}
            onChange={(e) => setMarket(e.target.value as Market | "ALL")}
          >
            <option value="ALL">{t.filterAll}</option>
            <option value="KR">{t.marketKR}</option>
            <option value="US">{t.marketUS}</option>
            <option value="CRYPTO">{t.marketCRYPTO}</option>
          </select>
        </label>
        <label>
          {t.filterAction}
          <select
            value={actionFilter}
            onChange={(e) => setActionFilter(e.target.value as CandidateAction | "ALL")}
          >
            {ACTIONS.map((a) => (
              <option key={a} value={a}>
                {a === "ALL" ? t.filterAll : t[ACTION_LABEL_KEY[a as CandidateAction]]}
              </option>
            ))}
          </select>
        </label>
      </div>

      {error && <div className={styles.error}>{t.warningPrefix}: {error}</div>}

      {data && data.warnings.length > 0 && (
        <ul className={styles.warnings}>
          {data.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}

      {data && filtered.length === 0 && (
        <div className={styles.empty}>{t.empty}</div>
      )}

      {filtered.length > 0 && (
        <table className={styles.table}>
          <thead>
            <tr>
              <th>{t.colSymbol}</th>
              <th>{t.colWeight}</th>
              <th>{t.colProfit}</th>
              <th>액션</th>
              <th>{t.colDecision}</th>
              <th>{t.colVerdict}</th>
              <th>{t.colSupport}</th>
              <th>{t.colResistance}</th>
              <th>{t.colJournal}</th>
              <th>{t.colReasons}</th>
              <th>{t.colMissing}</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((c) => (
              <PortfolioActionRow key={c.symbol} candidate={c} />
            ))}
          </tbody>
        </table>
      )}
    </main>
  );
}

import { useState } from "react";

import CandidateRow from "../components/CandidateRow";
import { screenCandidates } from "../api/candidates";
import { createSession } from "../api/researchPipeline";
import type {
  CandidateMarket,
  CandidateScreenRequest,
  CandidateScreenResponse,
  CandidateSortBy,
  CandidateStrategy,
  ScreenedCandidate,
} from "../api/types";
import { candidates as t } from "../i18n/ko";
import styles from "./CandidatesPage.module.css";

export default function CandidatesPage() {
  const [market, setMarket] = useState<CandidateMarket>("crypto");
  const [strategy, setStrategy] = useState<CandidateStrategy | "">("oversold");
  const [sortBy, setSortBy] = useState<CandidateSortBy | "">("");
  const [limit, setLimit] = useState<number>(30);
  const [krwOnly, setKrwOnly] = useState<boolean>(true);
  const [excludeWarnings, setExcludeWarnings] = useState<boolean>(false);

  const [data, setData] = useState<CandidateScreenResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [busySymbols, setBusySymbols] = useState<Set<string>>(new Set());
  const [confirmation, setConfirmation] = useState<string | null>(null);

  async function runScreen(): Promise<void> {
    setLoading(true);
    setError(null);
    setConfirmation(null);
    try {
      const body: CandidateScreenRequest = {
        market,
        strategy: strategy || null,
        sort_by: sortBy || null,
        limit,
        krw_only: krwOnly,
        exclude_warnings: excludeWarnings,
      };
      const res = await screenCandidates(body);
      setData(res);
    } catch (err) {
      setError(t.loadError);
    } finally {
      setLoading(false);
    }
  }

  async function handleStartResearch(c: ScreenedCandidate): Promise<void> {
    setBusySymbols((prev) => new Set(prev).add(c.symbol));
    setConfirmation(null);
    try {
      const res = await createSession({
        symbol: c.symbol,
        name: c.name ?? c.symbol,
        instrument_type: c.instrument_type ?? market,
        triggered_by: "user",
      });
      setConfirmation(`${t.researchStarted}${res.session_id})`);
    } catch (err) {
      setError(t.researchFailed);
    } finally {
      setBusySymbols((prev) => {
        const next = new Set(prev);
        next.delete(c.symbol);
        return next;
      });
    }
  }

  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <h1>{t.pageTitle}</h1>
        <p>{t.pageSubtitle}</p>
      </header>

      <form
        className={styles.filters}
        onSubmit={(e) => {
          e.preventDefault();
          runScreen();
        }}
      >
        <label>
          {t.filterMarket}
          <select value={market} onChange={(e) => setMarket(e.target.value as CandidateMarket)}>
            <option value="crypto">{t.marketCrypto}</option>
            <option value="kr">{t.marketKr}</option>
            <option value="us">{t.marketUs}</option>
          </select>
        </label>
        <label>
          {t.filterStrategy}
          <select value={strategy} onChange={(e) => setStrategy(e.target.value as CandidateStrategy | "")}>
            <option value="">{t.strategyAny}</option>
            <option value="oversold">{t.strategyOversold}</option>
            <option value="momentum">{t.strategyMomentum}</option>
            <option value="high_volume">{t.strategyHighVolume}</option>
          </select>
        </label>
        <label>
          {t.filterSort}
          <select value={sortBy} onChange={(e) => setSortBy(e.target.value as CandidateSortBy | "")}>
            <option value="">{t.sortAuto}</option>
            <option value="volume">{t.sortVolume}</option>
            <option value="trade_amount">{t.sortTradeAmount}</option>
            <option value="change_rate">{t.sortChangeRate}</option>
            <option value="rsi">{t.sortRsi}</option>
          </select>
        </label>
        <label>
          {t.filterLimit}
          <input
            type="number"
            min={1}
            max={100}
            value={limit}
            onChange={(e) => setLimit(Math.max(1, Math.min(100, Number(e.target.value) || 1)))}
          />
        </label>
        <label className={styles.checkbox}>
          <input type="checkbox" checked={krwOnly} onChange={(e) => setKrwOnly(e.target.checked)} />
          {t.filterKrwOnly}
        </label>
        <label className={styles.checkbox}>
          <input
            type="checkbox"
            checked={excludeWarnings}
            onChange={(e) => setExcludeWarnings(e.target.checked)}
          />
          {t.filterExcludeWarnings}
        </label>
        <button type="submit" disabled={loading}>
          {t.runScreen}
        </button>
      </form>

      {error && <div className={styles.error}>{error}</div>}
      {confirmation && <div className={styles.confirm}>{confirmation}</div>}

      {data && data.warnings.length > 0 && (
        <section className={styles.warnings}>
          <h3>{t.warningsHeader}</h3>
          <ul>
            {data.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
          <small>
            {t.rsiEnrichmentNote}
            {data.rsi_enrichment_succeeded}/{data.rsi_enrichment_attempted}
          </small>
        </section>
      )}

      {data && data.candidates.length === 0 && (
        <div className={styles.empty}>{t.empty}</div>
      )}

      {data && data.candidates.length > 0 && (
        <table className={styles.table}>
          <thead>
            <tr>
              <th>{t.colSymbol}</th>
              <th>{t.colPrice}</th>
              <th>{t.colChange}</th>
              <th>{t.colVolume}</th>
              <th>{t.colTradeAmount}</th>
              <th>{t.colVolumeRatio}</th>
              <th>{t.colRsi}</th>
              <th>{t.colMarketCap}</th>
              <th>{t.colHeld}</th>
              <th>{t.colWarnings}</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {data.candidates.map((c) => (
              <CandidateRow
                key={c.symbol}
                candidate={c}
                onStartResearch={handleStartResearch}
                busy={busySymbols.has(c.symbol)}
              />
            ))}
          </tbody>
        </table>
      )}
    </main>
  );
}

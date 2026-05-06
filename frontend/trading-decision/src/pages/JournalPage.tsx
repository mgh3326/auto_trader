import React, { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { tradeJournal, COMMON } from "../i18n";
import { JournalModal } from "../components/JournalModal";
import styles from "./JournalPage.module.css";

export default function JournalPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const marketFilter = searchParams.get("market") || "ALL";

  const [loading, setLoading] = useState(true);
  const [data, setData] = useState<any>(null);
  const [selectedRow, setSelectedRow] = useState<any>(null);
  const [isModalOpen, setIsModalOpen] = useState(false);

  const fetchData = async () => {
    setLoading(true);
    try {
      const url = marketFilter === "ALL" 
        ? "/api/v1/trade-journals/coverage"
        : `/api/v1/trade-journals/coverage?market=${marketFilter}`;
      const res = await fetch(url);
      if (!res.ok) throw new Error("Load failed");
      setData(await res.json());
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, [marketFilter]);

  const handleSave = async (payload: any) => {
    const isUpdate = !!selectedRow.journal_id;
    const url = isUpdate 
      ? `/api/v1/trade-journals/${selectedRow.journal_id}`
      : "/api/v1/trade-journals";
    const method = isUpdate ? "PATCH" : "POST";
    
    const body = isUpdate ? payload : {
      ...payload,
      symbol: selectedRow.symbol,
      instrument_type: selectedRow.instrument_type,
    };

    const res = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (!res.ok) throw new Error("Save failed");
    await fetchData();
  };

  if (loading) return <div className={styles.loading}>{COMMON.loading}</div>;
  if (!data) return <div className={styles.error}>{tradeJournal.loadError}</div>;

  return (
    <div className={styles.container}>
      <header className={styles.header}>
        <div>
          <h1>{tradeJournal.pageTitle}</h1>
          <p>{tradeJournal.pageSubtitle}</p>
        </div>
        <div className={styles.filters}>
          <label>{tradeJournal.filterMarket}</label>
          <select 
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
          {data.rows.map((row: any) => (
            <tr key={row.symbol} className={row.thesis_conflict_with_summary ? styles.conflictRow : ""}>
              <td>
                <div className={styles.symbolCell}>
                  <span className={styles.symbol}>{row.symbol}</span>
                  <span className={styles.name}>{row.name}</span>
                </div>
              </td>
              <td>{row.position_weight_pct?.toFixed(1)}%</td>
              <td>
                <span className={`${styles.statusBadge} ${styles[`status_${row.journal_status}`]}`}>
                  {tradeJournal[`status${row.journal_status.charAt(0).toUpperCase() + row.journal_status.slice(1)}` as keyof typeof tradeJournal]}
                </span>
              </td>
              <td className={styles.thesisCell}>{row.thesis || "—"}</td>
              <td>{row.target_price || "—"}</td>
              <td>{row.stop_loss || "—"}</td>
              <td>{row.min_hold_days ? `${row.min_hold_days}d` : "—"}</td>
              <td>
                {row.latest_summary_decision && (
                  <span className={`${styles.decisionBadge} ${styles[`decision_${row.latest_summary_decision}`]}`}>
                    {row.latest_summary_decision.toUpperCase()}
                  </span>
                )}
              </td>
              <td>
                <button 
                  onClick={() => {
                    setSelectedRow(row);
                    setIsModalOpen(true);
                  }}
                >
                  {row.journal_id ? tradeJournal.actionEdit : tradeJournal.actionCreate}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {data.total === 0 && <div className={styles.empty}>{tradeJournal.empty}</div>}

      {isModalOpen && (
        <JournalModal
          isOpen={isModalOpen}
          onClose={() => setIsModalOpen(false)}
          onSave={handleSave}
          initialData={selectedRow.journal_id ? {
            id: selectedRow.journal_id,
            thesis: selectedRow.thesis,
            target_price: selectedRow.target_price,
            stop_loss: selectedRow.stop_loss,
            min_hold_days: selectedRow.min_hold_days,
            // ... rest of fields if needed
          } : null}
        />
      )}
    </div>
  );
};

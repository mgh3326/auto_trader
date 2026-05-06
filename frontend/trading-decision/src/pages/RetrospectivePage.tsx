import React, { useEffect, useState } from "react";
import { retrospective, COMMON, SIDE_LABEL } from "../i18n";
import styles from "./RetrospectivePage.module.css";

export default function RetrospectivePage() {
  const [loading, setLoading] = useState(true);
  const [data, setData] = useState<any[]>([]);

  const fetchData = async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/v1/trade-journals/retrospective");
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
  }, []);

  if (loading) return <div className={styles.loading}>{COMMON.loading}</div>;

  return (
    <div className={styles.container}>
      <header className={styles.header}>
        <h1>{retrospective.pageTitle}</h1>
        <p>{retrospective.pageSubtitle}</p>
      </header>

      <table className={styles.table}>
        <thead>
          <tr>
            <th>{retrospective.colSymbol}</th>
            <th>{retrospective.colSide}</th>
            <th>{retrospective.colThesis}</th>
            <th>{retrospective.colPnL}</th>
            <th>{retrospective.colStatus}</th>
            <th>{retrospective.colDate}</th>
          </tr>
        </thead>
        <tbody>
          {data.map((row: any) => (
            <tr key={row.id}>
              <td><span className={styles.symbol}>{row.symbol}</span></td>
              <td>{SIDE_LABEL[row.side as keyof typeof SIDE_LABEL]}</td>
              <td className={styles.thesisCell}>{row.thesis}</td>
              <td className={row.pnl_pct >= 0 ? styles.profit : styles.loss}>
                {row.pnl_pct ? `${row.pnl_pct.toFixed(2)}%` : "—"}
              </td>
              <td>
                <span className={`${styles.statusBadge} ${styles[`status_${row.status}`]}`}>
                  {retrospective[`status${row.status.charAt(0).toUpperCase() + row.status.slice(1)}` as keyof typeof retrospective]}
                </span>
              </td>
              <td>{new Date(row.updated_at).toLocaleDateString()}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {data.length === 0 && <div className={styles.empty}>{retrospective.empty}</div>}
    </div>
  );
};

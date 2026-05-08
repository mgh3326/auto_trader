import "./screener.css";
import type { ScreenerResultRow } from "../../types/screener";

interface Props {
  rows: ScreenerResultRow[];
  metricLabel: string;
}

const directionClass: Record<ScreenerResultRow["changeDirection"], string> = {
  up: "screener-change-up",
  down: "screener-change-down",
  flat: "screener-change-flat",
};

export function ScreenerResultsTable({ rows, metricLabel }: Props) {
  if (rows.length === 0) {
    return <div className="screener-empty">표시할 종목이 없습니다.</div>;
  }
  return (
    <table className="screener-table" data-testid="screener-results-table">
      <thead>
        <tr>
          <th aria-label="관심" />
          <th>순위</th>
          <th>종목</th>
          <th>현재가</th>
          <th>등락률</th>
          <th>카테고리</th>
          <th>시가총액</th>
          <th>거래량</th>
          <th>애널리스트 분석</th>
          <th>{metricLabel}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={`${r.market}-${r.symbol}`} data-testid="screener-row">
            <td>
              <span
                className={r.isWatched ? "screener-heart-on" : "screener-heart-off"}
                aria-label={r.isWatched ? "관심 종목" : "관심 종목 아님"}
                role="img"
              >
                ♥
              </span>
            </td>
            <td>{r.rank}</td>
            <td className="screener-name-cell">
              <span className="screener-symbol-badge">{r.symbol}</span>
              <span className="screener-symbol-name">{r.name}</span>
            </td>
            <td>{r.priceLabel}</td>
            <td className={directionClass[r.changeDirection]}>
              {r.changePctLabel}
              <span className="screener-change-amount">{r.changeAmountLabel}</span>
            </td>
            <td>{r.category}</td>
            <td>{r.marketCapLabel}</td>
            <td>{r.volumeLabel}</td>
            <td>{r.analystLabel}</td>
            <td>{r.metricValueLabel}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

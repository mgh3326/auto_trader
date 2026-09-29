import type { TraderFillsResponse } from "./types";
import { fmtDateTime, fmtNum } from "./format";

export function FillsTodayPanel({
  data,
  loading,
  error,
  onRefresh,
}: {
  data: TraderFillsResponse | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}) {
  return (
    <div className="trader-panel" data-testid="panel-fills">
      <header className="trader-panel-head">
        <h2>오늘의 체결 (KST{data ? ` · ${data.day_kst}` : ""})</h2>
        <button type="button" onClick={onRefresh} disabled={loading}>
          {loading ? "조회 중…" : "새로고침"}
        </button>
      </header>
      {data
        ? data.notes.map((note) => (
            <p key={note} className="trader-note" data-testid="toss-fills-note">
              {note}
            </p>
          ))
        : null}
      {error ? <p className="trader-error">조회 실패: {error}</p> : null}
      {data && data.items.length === 0 ? (
        <p className="trader-dim">{data.empty_reason ?? "오늘 체결 없음"}</p>
      ) : null}
      {data && data.items.length > 0 ? (
        <table className="trader-table">
          <thead>
            <tr>
              <th>시각</th>
              <th>브로커</th>
              <th>종목</th>
              <th>구분</th>
              <th>수량</th>
              <th>가격</th>
              <th>금액</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((row) => (
              <tr key={`${row.broker}-${row.broker_order_id}-${row.fill_seq}`}>
                <td>{fmtDateTime(row.filled_at)}</td>
                <td>{row.broker}</td>
                <td>
                  {row.symbol_name ? `${row.symbol_name} ` : ""}
                  <span className="trader-dim">{row.symbol}</span>
                </td>
                <td>{row.side === "buy" ? "매수" : row.side === "sell" ? "매도" : row.side}</td>
                <td className="num">{fmtNum(row.filled_qty)}</td>
                <td className="num">{fmtNum(row.filled_price)}</td>
                <td className="num">
                  {fmtNum(row.filled_notional)} {row.currency}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {!data && !error && !loading ? <p className="trader-dim">불러오는 중…</p> : null}
    </div>
  );
}

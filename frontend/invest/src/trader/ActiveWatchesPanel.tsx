import type { TraderWatchRow, TraderWatchesResponse } from "./types";
import { fmtDateTime, fmtNum } from "./format";

const OPERATOR_LABEL: Record<TraderWatchRow["operator"], string> = {
  above: "≥",
  below: "≤",
  between: "구간",
};

export function ActiveWatchesPanel({
  data,
  loading,
  error,
  onRefresh,
}: {
  data: TraderWatchesResponse | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}) {
  return (
    <div className="trader-panel" data-testid="panel-watches">
      <header className="trader-panel-head">
        <h2>활성 감시{data ? ` (${data.count})` : ""}</h2>
        <button type="button" onClick={onRefresh} disabled={loading}>
          {loading ? "조회 중…" : "새로고침"}
        </button>
      </header>
      {error ? <p className="trader-error">조회 실패: {error}</p> : null}
      {data && data.items.length === 0 ? (
        <p className="trader-dim">활성 감시 없음</p>
      ) : null}
      {data && data.items.length > 0 ? (
        <table className="trader-table">
          <thead>
            <tr>
              <th>시장</th>
              <th>종목</th>
              <th>의도</th>
              <th>조건</th>
              <th>임계값</th>
              <th>유효기한</th>
              <th>모드</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((row) => (
              <tr key={row.alert_uuid}>
                <td>{row.market}</td>
                <td>{row.symbol}</td>
                <td>{row.intent}</td>
                <td>
                  {row.metric} {OPERATOR_LABEL[row.operator] ?? row.operator}
                </td>
                <td className="num">
                  {fmtNum(row.threshold)}
                  {row.threshold_high !== null ? ` ~ ${fmtNum(row.threshold_high)}` : ""}
                </td>
                <td>{fmtDateTime(row.valid_until)}</td>
                <td>{row.action_mode}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {!data && !error && !loading ? <p className="trader-dim">불러오는 중…</p> : null}
    </div>
  );
}

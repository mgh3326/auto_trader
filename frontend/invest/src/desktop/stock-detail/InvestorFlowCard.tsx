import { Card, Pill } from "../../ds";
import type {
  InvestorFlowDetailState,
  StockDetailInvestorFlow,
  StockDetailInvestorFlowDailyRow,
} from "../../types/stockDetail";

const STATE_TONE: Record<InvestorFlowDetailState, "gain" | "warn" | "paper"> = {
  fresh: "gain",
  stale: "warn",
  missing: "paper",
};

const STATE_LABEL: Record<InvestorFlowDetailState, string> = {
  fresh: "최신",
  stale: "지연",
  missing: "데이터 준비중",
};

function fmtSignedShares(v: number | null | undefined): string {
  if (v == null) return "−";
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${sign}${Math.abs(v).toLocaleString("ko-KR")}주`;
}

function streakLabel(buy: number | null | undefined, sell: number | null | undefined): string {
  if ((buy ?? 0) >= 1) return `+${buy}일 순매수`;
  if ((sell ?? 0) >= 1) return `−${sell}일 순매도`;
  return "연속성 없음";
}

function sourceLabel(data: StockDetailInvestorFlow): string {
  const rows = data.dailyRows ?? [];
  const source = data.snapshotSource ?? rows[0]?.source ?? "investor_flow_snapshots";
  const collected = data.collectedAt
    ? ` · 수집 ${new Date(data.collectedAt).toLocaleString("ko-KR")}`
    : "";
  const basis = data.snapshotDate ? `기준일 ${data.snapshotDate}` : "기준일 없음";
  return `${source} · ${basis}${collected} · delayed/read-only`;
}

function FlowMetric({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div style={{ border: "1px solid var(--line)", borderRadius: 14, padding: 12 }}>
      <div style={{ color: "var(--fg-3)", fontSize: 11 }}>{label}</div>
      <div style={{ marginTop: 4, fontWeight: 800, fontSize: 16 }}>{value}</div>
      <div style={{ marginTop: 3, color: "var(--fg-3)", fontSize: 11 }}>{sub}</div>
    </div>
  );
}

function RowTonePill({ row }: { row: StockDetailInvestorFlowDailyRow }) {
  if (row.doubleBuy) return <Pill tone="gain">쌍끌이</Pill>;
  if (row.doubleSell) return <Pill tone="loss">동반 매도</Pill>;
  return <Pill tone="paper">관찰</Pill>;
}

function DailyRowsTable({ rows }: { rows: StockDetailInvestorFlowDailyRow[] }) {
  if (rows.length === 0) {
    return (
      <p style={{ margin: "12px 0 0", color: "var(--fg-3)", fontSize: 12 }}>
        일별 투자자별 수급 행이 아직 적재되지 않았습니다.
      </p>
    );
  }
  return (
    <div style={{ marginTop: 12, overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead>
          <tr style={{ color: "var(--fg-3)", textAlign: "right" }}>
            <th style={{ padding: "6px 4px", textAlign: "left", borderBottom: "1px solid var(--line)" }}>일자</th>
            <th style={{ padding: "6px 4px", borderBottom: "1px solid var(--line)" }}>외국인</th>
            <th style={{ padding: "6px 4px", borderBottom: "1px solid var(--line)" }}>기관</th>
            <th style={{ padding: "6px 4px", borderBottom: "1px solid var(--line)" }}>개인</th>
            <th style={{ padding: "6px 4px", textAlign: "center", borderBottom: "1px solid var(--line)" }}>메모</th>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 10).map((row) => (
            <tr key={`${row.snapshotDate}-${row.source ?? "snapshot"}`}>
              <td style={{ padding: "7px 4px", borderBottom: "1px solid var(--line)", color: "var(--fg-2)" }}>{row.snapshotDate}</td>
              <td style={{ padding: "7px 4px", textAlign: "right", borderBottom: "1px solid var(--line)", color: (row.foreignNet ?? 0) >= 0 ? "var(--gain)" : "var(--loss)" }}>{fmtSignedShares(row.foreignNet)}</td>
              <td style={{ padding: "7px 4px", textAlign: "right", borderBottom: "1px solid var(--line)", color: (row.institutionNet ?? 0) >= 0 ? "var(--gain)" : "var(--loss)" }}>{fmtSignedShares(row.institutionNet)}</td>
              <td style={{ padding: "7px 4px", textAlign: "right", borderBottom: "1px solid var(--line)", color: (row.individualNet ?? 0) >= 0 ? "var(--gain)" : "var(--loss)" }}>{fmtSignedShares(row.individualNet)}</td>
              <td style={{ padding: "7px 4px", textAlign: "center", borderBottom: "1px solid var(--line)" }}><RowTonePill row={row} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function InvestorFlowCard({ data }: { data: StockDetailInvestorFlow }) {
  const dailyRows = data.dailyRows ?? [];
  const isMissing = data.dataState === "missing";
  return (
    <Card data-testid="stock-detail-investor-flow">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
        <div>
          <strong>투자자별 매매동향 · 수급 흐름</strong>
          <p style={{ margin: "4px 0 0", color: "var(--fg-3)", fontSize: 12 }}>{sourceLabel(data)}</p>
        </div>
        <Pill tone={STATE_TONE[data.dataState]}>{STATE_LABEL[data.dataState]}</Pill>
      </div>
      {data.dataState === "stale" ? (
        <p style={{ margin: "10px 0 0", color: "var(--warn)", fontSize: 12 }}>
          최신 거래일보다 오래된 스냅샷입니다. 방향성 참고용으로만 확인하세요.
        </p>
      ) : null}
      {isMissing ? (
        <p style={{ marginTop: 12, color: "var(--fg-3)", fontSize: 12 }}>
          최근 KR 투자자별 수급 스냅샷이 없어 표시할 수 없습니다.
        </p>
      ) : (
        <>
          <div
            style={{
              marginTop: 12,
              display: "grid",
              gridTemplateColumns: "repeat(3, minmax(0, 1fr))",
              gap: 12,
            }}
          >
            <FlowMetric
              label="외국인 순매수"
              value={fmtSignedShares(data.foreignNet)}
              sub={streakLabel(data.foreignConsecutiveBuyDays, data.foreignConsecutiveSellDays)}
            />
            <FlowMetric
              label="기관 순매수"
              value={fmtSignedShares(data.institutionNet)}
              sub={streakLabel(data.institutionConsecutiveBuyDays, data.institutionConsecutiveSellDays)}
            />
            <FlowMetric
              label="개인 순매수"
              value={fmtSignedShares(data.individualNet)}
              sub={streakLabel(data.individualConsecutiveBuyDays, data.individualConsecutiveSellDays)}
            />
          </div>
          {(data.doubleBuy || data.doubleSell) && (
            <div style={{ marginTop: 10 }}>
              <Pill tone={data.doubleBuy ? "gain" : "loss"}>
                {data.doubleBuy ? "외국인·기관 동반 순매수" : "외국인·기관 동반 순매도"}
              </Pill>
            </div>
          )}
          <DailyRowsTable rows={dailyRows} />
        </>
      )}
      <p style={{ marginTop: 12, color: "var(--fg-3)", fontSize: 11 }}>
        {data.cautionLabel}
      </p>
    </Card>
  );
}

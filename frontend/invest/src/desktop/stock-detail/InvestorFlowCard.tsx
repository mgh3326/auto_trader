import { Card, Pill } from "../../ds";
import type { InvestorFlowDetailState, StockDetailInvestorFlow } from "../../types/stockDetail";

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

function fmtKrwSigned(v: number | null | undefined): string {
  if (v == null) return "−";
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${sign}₩${Math.abs(v).toLocaleString("ko-KR")}`;
}

function streakLabel(buy: number | null | undefined, sell: number | null | undefined): string {
  if ((buy ?? 0) >= 1) return `+${buy}일 순매수`;
  if ((sell ?? 0) >= 1) return `−${sell}일 순매도`;
  return "−";
}

function FlowMetric({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div>
      <div style={{ color: "var(--fg-3)", fontSize: 11 }}>{label}</div>
      <div style={{ fontWeight: 700, fontSize: 16 }}>{value}</div>
      <div style={{ color: "var(--fg-3)", fontSize: 11 }}>{sub}</div>
    </div>
  );
}

export function InvestorFlowCard({ data }: { data: StockDetailInvestorFlow }) {
  return (
    <Card data-testid="stock-detail-investor-flow">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <strong>투자자별 수급 ({data.snapshotDate ?? "−"})</strong>
        <Pill tone={STATE_TONE[data.dataState]}>{STATE_LABEL[data.dataState]}</Pill>
      </div>
      {data.dataState === "missing" ? (
        <p style={{ marginTop: 8, color: "var(--fg-3)", fontSize: 12 }}>
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
              value={fmtKrwSigned(data.foreignNet)}
              sub={streakLabel(data.foreignConsecutiveBuyDays, data.foreignConsecutiveSellDays)}
            />
            <FlowMetric
              label="기관 순매수"
              value={fmtKrwSigned(data.institutionNet)}
              sub={streakLabel(data.institutionConsecutiveBuyDays, data.institutionConsecutiveSellDays)}
            />
            <FlowMetric
              label="개인 순매수"
              value={fmtKrwSigned(data.individualNet)}
              sub={streakLabel(data.individualConsecutiveBuyDays, data.individualConsecutiveSellDays)}
            />
          </div>
          {(data.doubleBuy || data.doubleSell) && (
            <div style={{ marginTop: 10 }}>
              <Pill tone={data.doubleBuy ? "gain" : "loss"}>
                {data.doubleBuy ? "쌍끌이 매수" : "쌍끌이 매도"}
              </Pill>
            </div>
          )}
        </>
      )}
      <p style={{ marginTop: 12, color: "var(--fg-3)", fontSize: 11 }}>
        {data.cautionLabel}
      </p>
    </Card>
  );
}

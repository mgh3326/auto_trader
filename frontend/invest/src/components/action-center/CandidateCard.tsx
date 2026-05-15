import { Button, Card } from "../../ds";
import type { AnalysisCandidate } from "../../types/actionCenter";
import { StatusBadge } from "./StatusBadge";

const UNAVAILABLE = "확인 불가";

function displayValue(value: unknown, suffix = ""): string {
  if (value == null || value === "") return UNAVAILABLE;
  return `${String(value)}${suffix}`;
}

function verificationValue(candidate: AnalysisCandidate, key: string): string {
  const raw = candidate.verification?.[key];
  if (raw == null || raw === "") return UNAVAILABLE;
  return String(raw);
}

export function CandidateCard({ candidate }: { candidate: AnalysisCandidate }) {
  return (
    <Card>
      <div style={{ display: "grid", gap: 14 }}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start" }}>
          <div>
            <div style={{ color: "var(--fg-3)", fontSize: 12, fontWeight: 800 }}>
              {candidate.market} · {candidate.actionType} · priority {candidate.priority}
            </div>
            <h3 style={{ margin: "4px 0 0", fontSize: 22, letterSpacing: "-0.03em" }}>{candidate.symbol}</h3>
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "flex-end" }}>
            <StatusBadge label="승인" status={candidate.approvalStatus} />
            <StatusBadge label="실행" status={candidate.executionState} />
          </div>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))", gap: 8 }}>
          <Metric label="side" value={candidate.side} />
          <Metric label="수량" value={displayValue(candidate.quantity)} />
          <Metric label="비중" value={displayValue(candidate.quantityPct, "%")} />
          <Metric label="지정가" value={displayValue(candidate.limitPrice)} />
          <Metric label="금액" value={displayValue(candidate.notional, candidate.currency ? ` ${candidate.currency}` : "")} />
          <Metric label="confidence" value={displayValue(candidate.confidence)} />
        </div>

        <div>
          <div style={{ fontWeight: 900, marginBottom: 6 }}>Thesis</div>
          <p style={{ margin: 0, color: "var(--fg-2)", fontSize: 13, lineHeight: 1.6 }}>{candidate.thesis}</p>
        </div>

        <div style={{ display: "grid", gap: 8 }}>
          <div style={{ fontWeight: 900 }}>검증/리스크</div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 8 }}>
            <Metric label="계좌 feasibility" value={verificationValue(candidate, "accountFeasibility")} warn />
            <Metric label="시장/유동성" value={verificationValue(candidate, "liquidity")} warn />
            <Metric label="이벤트/뉴스 리스크" value={verificationValue(candidate, "eventNewsRisk")} warn />
          </div>
          {candidate.riskNotes.length > 0 && (
            <div style={{ color: "var(--warn)", fontSize: 12, lineHeight: 1.6 }}>{candidate.riskNotes.join(" · ")}</div>
          )}
          {candidate.blockingReasons.length > 0 && (
            <div style={{ color: "var(--danger)", fontSize: 12, lineHeight: 1.6 }}>{candidate.blockingReasons.join(" · ")}</div>
          )}
        </div>

        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <Button type="button" variant="secondary" disabled>승인 기록은 수동 처리</Button>
          <Button type="button" variant="ghost" disabled>거절 기록은 수동 처리</Button>
          <div style={{ color: "var(--fg-3)", fontSize: 12, display: "flex", gap: 10, flexWrap: "wrap" }}>
            <span>승인 상태: {candidate.approvalStatus}</span>
            <span>실행 상태: {candidate.executionState}</span>
          </div>
        </div>
      </div>
    </Card>
  );
}

function Metric({ label, value, warn = false }: { label: string; value: string; warn?: boolean }) {
  const unavailable = value === UNAVAILABLE;
  return (
    <div style={{ padding: 10, borderRadius: 12, background: "var(--surface-2)", display: "grid", gap: 3 }}>
      <div style={{ color: "var(--fg-3)", fontSize: 11 }}>{label}</div>
      <div style={{ color: warn || unavailable ? "var(--warn)" : "var(--fg-1)", fontWeight: 900, fontSize: 12 }}>{value}</div>
    </div>
  );
}

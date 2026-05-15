import { Card } from "../../ds";
import type { AnalysisReport, AnalysisStageResult } from "../../types/actionCenter";
import { StatusBadge } from "./StatusBadge";

const UNAVAILABLE = "확인 불가";

function valueText(value: unknown): string {
  if (value == null || value === "") return UNAVAILABLE;
  if (Array.isArray(value)) return value.length === 0 ? UNAVAILABLE : value.join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function KeyValueGrid({ values }: { values: Record<string, unknown> }) {
  const entries = Object.entries(values);
  if (entries.length === 0) {
    return <div style={{ color: "var(--fg-3)", fontSize: 12 }}>{UNAVAILABLE}</div>;
  }
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 8 }}>
      {entries.map(([key, value]) => (
        <div key={key} style={{ padding: 10, borderRadius: 12, background: "var(--surface-2)", display: "grid", gap: 3 }}>
          <div style={{ color: "var(--fg-3)", fontSize: 11 }}>{key}</div>
          <div style={{ color: valueText(value) === UNAVAILABLE ? "var(--warn)" : "var(--fg-1)", fontWeight: 800, fontSize: 12 }}>
            {valueText(value)}
          </div>
        </div>
      ))}
    </div>
  );
}

function StageRow({ stage }: { stage: AnalysisStageResult }) {
  return (
    <div style={{ padding: "9px 0", borderTop: "1px solid var(--divider)", display: "grid", gap: 4 }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "center" }}>
        <strong style={{ fontSize: 13 }}>{stage.stageKey}</strong>
        <StatusBadge status={stage.status} />
      </div>
      <div style={{ color: "var(--fg-3)", fontSize: 12 }}>
        {stage.source} · {stage.freshnessAt ?? stage.unavailableReason ?? UNAVAILABLE}
      </div>
      {stage.warnings && stage.warnings.length > 0 && (
        <div style={{ color: "var(--warn)", fontSize: 12 }}>{stage.warnings.join(" · ")}</div>
      )}
    </div>
  );
}

export function DataVerificationPanel({ report }: { report: AnalysisReport }) {
  return (
    <Card soft>
      <div style={{ display: "grid", gap: 12 }}>
        <div>
          <div style={{ fontWeight: 900, marginBottom: 4 }}>데이터 검증</div>
          <div style={{ color: "var(--fg-3)", fontSize: 12, lineHeight: 1.5 }}>
            KIS live가 계좌·주문 권한의 기준입니다. Toss/Naver는 교차 검증 참고이며, 확인되지 않은 핵심 값은 {UNAVAILABLE}로 표시합니다.
          </div>
        </div>
        <div>
          <div style={{ color: "var(--fg-3)", fontSize: 12, fontWeight: 800, marginBottom: 6 }}>Freshness</div>
          <KeyValueGrid values={report.dataFreshness ?? {}} />
        </div>
        <div>
          <div style={{ color: "var(--fg-3)", fontSize: 12, fontWeight: 800, marginBottom: 6 }}>Coverage</div>
          <KeyValueGrid values={report.coverage ?? {}} />
        </div>
        {report.stageResults && report.stageResults.length > 0 && (
          <div>
            <div style={{ color: "var(--fg-3)", fontSize: 12, fontWeight: 800, marginBottom: 2 }}>Stage checks</div>
            {report.stageResults.map((stage) => <StageRow key={`${stage.stageKey}:${stage.source}`} stage={stage} />)}
          </div>
        )}
      </div>
    </Card>
  );
}

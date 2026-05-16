import { Card } from "../../ds";
import type { AnalysisReport } from "../../types/actionCenter";
import { DataVerificationPanel } from "./DataVerificationPanel";
import { StatusBadge } from "./StatusBadge";

function dateText(value?: string | null): string {
  if (!value) return "확인 불가";
  return new Date(value).toLocaleString("ko-KR", { dateStyle: "short", timeStyle: "short" });
}

export function AnalystReportCard({ report }: { report: AnalysisReport }) {
  return (
    <Card>
      <div style={{ display: "grid", gap: 14 }}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start" }}>
          <div style={{ display: "grid", gap: 5, minWidth: 0 }}>
            <div style={{ color: "var(--fg-3)", fontSize: 12, fontWeight: 800 }}>
              {report.reportType} · {report.market} · {report.accountScope ?? "전체 계좌"}
            </div>
            <h2
              style={{
                margin: 0,
                fontSize: 18,
                letterSpacing: "-0.03em",
                lineHeight: 1.35,
                overflowWrap: "anywhere",
              }}
            >
              {report.summary}
            </h2>
            <div style={{ color: "var(--fg-3)", fontSize: 12 }}>
              {report.createdByProfile} · 생성 {dateText(report.createdAt)} · 유효 {dateText(report.validUntil)}
            </div>
          </div>
          <StatusBadge status={report.status} />
        </div>
        {report.riskSummary && (
          <p style={{ margin: 0, color: "var(--fg-2)", fontSize: 13, lineHeight: 1.6, overflowWrap: "anywhere" }}>
            {report.riskSummary}
          </p>
        )}
        <DataVerificationPanel report={report} />
        {report.safetyNotes && report.safetyNotes.length > 0 && (
          <div style={{ color: "var(--warn)", fontSize: 12, lineHeight: 1.6 }}>
            {report.safetyNotes.map((note) => <div key={note}>• {note}</div>)}
          </div>
        )}
      </div>
    </Card>
  );
}

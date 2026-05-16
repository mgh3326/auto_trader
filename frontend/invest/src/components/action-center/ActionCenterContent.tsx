import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { AnalystReportCard } from "./AnalystReportCard";
import { CandidateCard } from "./CandidateCard";
import { Card } from "../../ds";
import { useActionCenter } from "../../hooks/useActionCenter";
import type { AnalysisCandidate, AnalysisReport } from "../../types/actionCenter";

const MARKET_LABELS: Record<string, string> = {
  crypto: "코인",
  kr: "국내주식",
  us: "미국주식",
};

function SafetyHero() {
  return (
    <Card>
      <div style={{ display: "grid", gap: 9 }}>
        <div style={{ color: "var(--fg-3)", fontSize: 12, fontWeight: 900 }}>ROB-257 애널리스트 리포트 액션 센터</div>
        <h1 style={{ margin: 0, fontSize: 28, letterSpacing: "-0.05em" }}>액션 센터</h1>
        <p style={{ margin: 0, color: "var(--fg-2)", fontSize: 14, lineHeight: 1.65 }}>
          이 화면은 리포트별 판단 근거와 승인 후보를 묶어 보는 읽기 전용 검토 화면입니다. 승인·거절 기록은 현재 수동 처리이며,
          별도 승인 없이는 주문을 제출하지 않습니다.
        </p>
        <div style={{ color: "var(--warn)", fontSize: 13, fontWeight: 800 }}>
          후보 카드는 리포트 단위로 묶입니다 · 매수/매도 실행 전 계좌·호가·매도 가능 수량 재확인 필수
        </div>
      </div>
    </Card>
  );
}

function StatusCard({ children }: { children: ReactNode }) {
  return <Card><div role="status" style={{ color: "var(--fg-3)", fontSize: 13 }}>{children}</div></Card>;
}

function reportTime(report: AnalysisReport): number {
  return new Date(report.publishedAt ?? report.createdAt).getTime();
}

function reportTitle(report: AnalysisReport): string {
  const date = new Date(report.publishedAt ?? report.createdAt);
  const time = Number.isFinite(date.getTime())
    ? date.toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false })
    : "시각 확인 필요";
  const market = MARKET_LABELS[report.market] ?? report.market;
  return `${time} ${market} 리포트`;
}

function candidateKey(candidate: AnalysisCandidate): string {
  return `${candidate.symbol}:${candidate.actionType}:${candidate.side}`;
}

function diffText(current: AnalysisCandidate[], previous?: AnalysisCandidate[]): string {
  if (!previous) return "비교 기준이 되는 이전 리포트가 없습니다.";
  const previousByKey = new Map(previous.map((candidate) => [candidateKey(candidate), candidate]));
  const currentByKey = new Map(current.map((candidate) => [candidateKey(candidate), candidate]));
  const added = current.filter((candidate) => !previousByKey.has(candidateKey(candidate))).map((candidate) => candidate.symbol);
  const removed = previous.filter((candidate) => !currentByKey.has(candidateKey(candidate))).map((candidate) => candidate.symbol);
  const changed = current.filter((candidate) => {
    const before = previousByKey.get(candidateKey(candidate));
    return before && (before.approvalStatus !== candidate.approvalStatus || before.executionState !== candidate.executionState || before.priority !== candidate.priority);
  }).map((candidate) => candidate.symbol);

  const parts = [
    added.length ? `신규 ${Array.from(new Set(added)).join(", ")}` : null,
    removed.length ? `사라짐 ${Array.from(new Set(removed)).join(", ")}` : null,
    changed.length ? `상태/우선순위 변경 ${Array.from(new Set(changed)).join(", ")}` : null,
  ].filter(Boolean);

  return parts.length > 0 ? `이전 리포트 대비 ${parts.join(" · ")}` : "이전 리포트 대비 후보 구성 변화 없음";
}

function ReportBundle({ report, candidates, previousCandidates }: { report: AnalysisReport; candidates: AnalysisCandidate[]; previousCandidates?: AnalysisCandidate[] }) {
  return (
    <section style={{ display: "grid", gap: 10, padding: 12, border: "1px solid var(--border)", borderRadius: 18, background: "rgba(255,255,255,0.015)", minWidth: 0 }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 10, alignItems: "baseline", flexWrap: "wrap" }}>
        <div style={{ display: "grid", gap: 3, minWidth: 0 }}>
          <h2 style={{ margin: 0, fontSize: 18, letterSpacing: "-0.03em" }}>{reportTitle(report)}</h2>
          <div style={{ color: "var(--fg-3)", fontSize: 12, overflowWrap: "anywhere" }}>{diffText(candidates, previousCandidates)}</div>
        </div>
        <span style={{ color: "var(--fg-3)", fontSize: 12 }}>{candidates.length}개 후보</span>
      </div>

      <AnalystReportCard report={report} />

      <div style={{ display: "grid", gap: 10 }}>
        <div style={{ color: "var(--fg-2)", fontSize: 13, fontWeight: 900 }}>이 리포트의 승인/검토 후보</div>
        {candidates.length === 0 ? (
          <StatusCard>이 리포트에 연결된 후보가 없습니다.</StatusCard>
        ) : (
          candidates.map((candidate) => <CandidateCard key={candidate.candidateUuid} candidate={candidate} />)
        )}
      </div>
    </section>
  );
}

export function ActionCenterContent({ compact = false }: { compact?: boolean }) {
  const actionCenter = useActionCenter();
  const reports = actionCenter.state.status === "ready"
    ? [...actionCenter.state.reports.reports].sort((a, b) => reportTime(b) - reportTime(a))
    : [];
  const allCandidates = actionCenter.state.status === "ready" ? actionCenter.state.candidates.candidates : [];
  const candidatesByReport = new Map<string, AnalysisCandidate[]>();
  for (const candidate of allCandidates) {
    if (!candidate.reportUuid) continue;
    const current = candidatesByReport.get(candidate.reportUuid) ?? [];
    current.push(candidate);
    candidatesByReport.set(candidate.reportUuid, current);
  }

  return (
    <div style={{ padding: compact ? "14px 16px 22px" : 24, display: "grid", gap: 16 }}>
      <SafetyHero />

      {actionCenter.state.status === "loading" && <StatusCard>액션 센터 데이터를 불러오는 중… 주문 실행이 아닙니다.</StatusCard>}
      {actionCenter.state.status === "error" && (
        <Card>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center" }}>
            <div style={{ color: "var(--danger)", fontSize: 13 }}>액션 센터 데이터를 일시적으로 불러오지 못했습니다.</div>
            <button
              type="button"
              onClick={actionCenter.reload}
              style={{
                padding: "6px 12px",
                borderRadius: 10,
                border: "1px solid var(--border)",
                background: "var(--surface-2)",
                color: "var(--fg-1)",
                fontFamily: "inherit",
                fontWeight: 800,
                cursor: "pointer",
              }}
            >
              재시도
            </button>
          </div>
        </Card>
      )}

      {actionCenter.state.status === "ready" && (
        <section style={{ display: "grid", gap: 12 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 10 }}>
            <div style={{ display: "grid", gap: 3 }}>
              <h2 style={{ margin: 0, fontSize: 18 }}>리포트별 검토 이력</h2>
              <div style={{ color: "var(--fg-3)", fontSize: 12 }}>각 리포트와 그 리포트에서 생성된 후보를 함께 보여줍니다.</div>
            </div>
            <span style={{ color: "var(--fg-3)", fontSize: 12 }}>{reports.length}개 리포트</span>
          </div>
          {reports.length === 0 ? (
            <StatusCard>표시할 리포트가 없습니다.</StatusCard>
          ) : (
            reports.map((report, index) => {
              const previousReport = reports[index + 1];
              return (
                <ReportBundle
                  key={report.reportUuid}
                  report={report}
                  candidates={candidatesByReport.get(report.reportUuid) ?? []}
                  previousCandidates={previousReport ? candidatesByReport.get(previousReport.reportUuid) ?? [] : undefined}
                />
              );
            })
          )}
        </section>
      )}
    </div>
  );
}

export function ActionCenterRelatedLinks() {
  return (
    <Card>
      <div style={{ fontWeight: 900, marginBottom: 8 }}>관련 화면</div>
      <div style={{ display: "grid", gap: 8, fontSize: 13 }}>
        <Link to="/" style={{ color: "var(--fg-1)", textDecoration: "none" }}>홈</Link>
        <Link to="/insights" style={{ color: "var(--fg-1)", textDecoration: "none" }}>인사이트</Link>
        <Link to="/my?tab=signals" style={{ color: "var(--fg-1)", textDecoration: "none" }}>시그널</Link>
      </div>
    </Card>
  );
}

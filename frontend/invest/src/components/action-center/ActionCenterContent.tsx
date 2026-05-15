import { Link } from "react-router-dom";
import { AnalystReportCard } from "./AnalystReportCard";
import { CandidateCard } from "./CandidateCard";
import { Card } from "../../ds";
import { useActionCenter } from "../../hooks/useActionCenter";

function SafetyHero() {
  return (
    <Card>
      <div style={{ display: "grid", gap: 9 }}>
        <div style={{ color: "var(--fg-3)", fontSize: 12, fontWeight: 900 }}>ROB-257 analyst report action center</div>
        <h1 style={{ margin: 0, fontSize: 28, letterSpacing: "-0.05em" }}>액션 센터</h1>
        <p style={{ margin: 0, color: "var(--fg-2)", fontSize: 14, lineHeight: 1.65 }}>
          이 화면은 의사결정/승인 대기 자료이며 주문 실행이 아닙니다. 승인·거절은 현재 read-only 수동 검토 상태로 표시하고,
          KIS live를 계좌·주문 권한의 기준으로 둡니다.
        </p>
        <div style={{ color: "var(--warn)", fontSize: 13, fontWeight: 800 }}>
          정규장 확인 필요 · NXT/프리마켓/시간외 시장가 또는 시장가 유사 실행은 금지
        </div>
      </div>
    </Card>
  );
}

function StatusCard({ children }: { children: React.ReactNode }) {
  return <Card><div role="status" style={{ color: "var(--fg-3)", fontSize: 13 }}>{children}</div></Card>;
}

export function ActionCenterContent({ compact = false }: { compact?: boolean }) {
  const actionCenter = useActionCenter();

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
        <>
          <section style={{ display: "grid", gap: 10 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 10 }}>
              <h2 style={{ margin: 0, fontSize: 18 }}>최신 애널리스트 리포트</h2>
              <span style={{ color: "var(--fg-3)", fontSize: 12 }}>{actionCenter.state.reports.reports.length}개</span>
            </div>
            {actionCenter.state.reports.reports.length === 0 ? (
              <StatusCard>표시할 리포트가 없습니다. 확인 불가</StatusCard>
            ) : (
              actionCenter.state.reports.reports.map((report) => <AnalystReportCard key={report.reportUuid} report={report} />)
            )}
          </section>

          <section style={{ display: "grid", gap: 10 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 10 }}>
              <h2 style={{ margin: 0, fontSize: 18 }}>승인 대기 후보</h2>
              <span style={{ color: "var(--fg-3)", fontSize: 12 }}>{actionCenter.state.candidates.candidates.length}개</span>
            </div>
            {actionCenter.state.candidates.candidates.length === 0 ? (
              <StatusCard>표시할 후보가 없습니다. 확인 불가</StatusCard>
            ) : (
              actionCenter.state.candidates.candidates.map((candidate) => (
                <CandidateCard key={candidate.candidateUuid} candidate={candidate} />
              ))
            )}
          </section>
        </>
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

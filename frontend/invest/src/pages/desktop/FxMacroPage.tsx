import { Link } from "react-router-dom";
import { DesktopShell } from "../../desktop/DesktopShell";
import { Card } from "../../ds";
import { FxCollectionCard, FxDefenseSignalCard, FxDeferredSectionsCard, FxQuoteCard, FxSourceFreshnessList, FxStatePill, FxThresholdCard } from "../../components/fx/FxDashboardCards";
import { useFxDashboard } from "../../hooks/useFxDashboard";
import { useViewport } from "../../hooks/useViewport";
import type { FxDashboardResponse } from "../../types/fxDashboard";

function Header({ data, onReload }: { data?: FxDashboardResponse | null; onReload: () => void }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "start", flexWrap: "wrap" }}>
      <div>
        <h1 style={{ margin: 0, fontSize: 28, letterSpacing: "-0.05em" }}>FX·매크로</h1>
        <p style={{ margin: "6px 0 0", color: "var(--fg-2)", fontSize: 14 }}>
          USD/KRW, 글로벌 달러, 원화 교차, 사후 검증 상태를 표시만 합니다.
        </p>
        {data && <div style={{ color: "var(--fg-3)", fontSize: 12, marginTop: 6 }}>asOf {data.asOf}</div>}
      </div>
      <button
        type="button"
        onClick={onReload}
        style={{ padding: "8px 12px", borderRadius: 10, border: "1px solid var(--border)", background: "var(--surface)", color: "var(--fg-1)", fontWeight: 800, cursor: "pointer" }}
      >
        새로고침
      </button>
    </div>
  );
}

function StatusCard({ data }: { data: FxDashboardResponse }) {
  return (
    <Card>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <div>
          <div style={{ fontWeight: 900 }}>FX 대시보드 상태</div>
          <div style={{ color: "var(--fg-3)", fontSize: 12, marginTop: 4 }}>
            공급자 응답은 부분/누락/오류 상태를 숨기지 않고 표시합니다.
          </div>
        </div>
        <FxStatePill state={data.dataState} />
      </div>
      {data.warnings.length > 0 && (
        <div style={{ marginTop: 10, color: "var(--warn)", fontSize: 12 }}>{data.warnings.map((w) => `⚠ ${w}`).join(" · ")}</div>
      )}
    </Card>
  );
}

function SafetyCard() {
  return (
    <Card>
      <div style={{ fontWeight: 900, marginBottom: 8 }}>읽기 전용 원칙</div>
      <ul style={{ margin: 0, paddingLeft: 18, color: "var(--fg-2)", fontSize: 13, lineHeight: 1.7 }}>
        <li>주문·매매 API, watch/order intent, scheduler activation을 호출하지 않습니다.</li>
        <li>당국 개입은 확정 표현하지 않고 사후 검증 필요 여부만 표시합니다.</li>
        <li><Link to="/market" style={{ color: "inherit" }}>시장 대시보드</Link>와 동일한 읽기 전용 분석 영역입니다.</li>
      </ul>
    </Card>
  );
}

function FxContent({ data }: { data: FxDashboardResponse }) {
  return (
    <>
      <StatusCard data={data} />
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 16 }}>
        <FxQuoteCard metric={data.usdKrw} />
        <FxThresholdCard thresholds={data.thresholds} />
      </div>
      <FxDefenseSignalCard signal={data.defenseSignal} disclaimers={data.disclaimers} />
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 16 }}>
        <FxCollectionCard title="글로벌 달러" items={data.globalDollar} />
        <FxCollectionCard title="원화 교차" items={data.krwCrosses} />
      </div>
      <FxSourceFreshnessList sources={data.sourceFreshness} />
      <FxDeferredSectionsCard
        foreignFlow={data.foreignFlow}
        news={data.news}
        events={data.events}
        afterVerification={data.afterVerification}
      />
    </>
  );
}

function FxMain({ mobile = false }: { mobile?: boolean }) {
  const { state, reload } = useFxDashboard();
  const data = state.status === "ready" ? state.data : null;

  return (
    <div style={{ padding: mobile ? 16 : 24, display: "grid", gap: 16, maxWidth: mobile ? 860 : undefined, margin: mobile ? "0 auto" : undefined }}>
      <Header data={data} onReload={reload} />
      {state.status === "loading" && <Card>FX·매크로 데이터를 불러오는 중…</Card>}
      {state.status === "error" && (
        <Card><span style={{ color: "var(--danger)" }}>FX·매크로 데이터를 일시적으로 불러오지 못했습니다.</span></Card>
      )}
      {data && <FxContent data={data} />}
      {mobile && <SafetyCard />}
    </div>
  );
}

export function FxMacroRoute() {
  const viewport = useViewport();
  if (viewport === "mobile") {
    return (
      <main data-testid="fx-mobile-page" style={{ minHeight: "100vh", background: "var(--bg)", color: "var(--fg-1)" }}>
        <FxMain mobile />
      </main>
    );
  }

  return (
    <DesktopShell
      center={<FxMain />}
      right={<SafetyCard />}
    />
  );
}

import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { CommonPreferredDisparityCardView } from "../../components/CommonPreferredDisparityCard";
import { MarketParityStrip } from "../../components/home/MarketParityStrip";
import { AnalysisArtifactPanel } from "../../components/insights/AnalysisArtifactPanel";
import { ForecastCalibrationPanel } from "../../components/insights/ForecastCalibrationPanel";
import { SessionContextTimelinePanel } from "../../components/insights/SessionContextTimelinePanel";
import { PageSafetyNote } from "../../components/PageSafetyNote";
import { DesktopShell } from "../../desktop/DesktopShell";
import { Card } from "../../ds";
import { useCommonPreferredDisparity } from "../../hooks/useCommonPreferredDisparity";
import { useMarketParity } from "../../hooks/useMarketParity";

function SectionStatus({ children }: { children: ReactNode }) {
  return (
    <div
      role="status"
      style={{
        padding: 14,
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: 14,
        color: "var(--fg-3)",
        fontSize: 13,
      }}
    >
      {children}
    </div>
  );
}

function DecisionCard() {
  return (
    <Card>
      <div style={{ display: "grid", gap: 8 }}>
        <div style={{ fontSize: 12, color: "var(--fg-3)", fontWeight: 800 }}>ROB-253 decision</div>
        <h1 style={{ margin: 0, fontSize: 28, letterSpacing: "-0.05em" }}>인사이트</h1>
        <p style={{ margin: 0, color: "var(--fg-2)", fontSize: 14, lineHeight: 1.6 }}>
          홈에는 compact 요약만 남기고, 괴리·패리티처럼 해석 주의가 필요한 read-only 관찰 카드는
          /invest/insights에서 모아 봅니다. 종목별 리서치 컨센서스는 symbol context가 필요하므로
          개별 종목 상세 화면에 유지합니다.
        </p>
      </div>
    </Card>
  );
}

function ReadOnlyGuardrailNote() {
  return (
    <PageSafetyNote
      routeId="insights"
      heading="읽기 전용 가드레일"
      tag="인사이트"
      items={[
        "주문·매매·watch mutation API를 호출하지 않습니다.",
        "기존 read-only /invest/api 응답만 표시합니다.",
        "데이터 수집 활성화·백필·production DB write는 별도 승인 후 진행합니다.",
        "raoni.xyz API에 production 의존성을 추가하지 않습니다.",
      ]}
    />
  );
}

function RelatedScreensCard() {
  return (
    <Card>
      <div style={{ fontWeight: 900, marginBottom: 8 }}>관련 화면</div>
      <div style={{ display: "grid", gap: 8, fontSize: 13 }}>
        <Link to="/" style={{ color: "var(--fg-1)", textDecoration: "none" }}>홈 compact 요약</Link>
        <Link to="/market" style={{ color: "var(--fg-1)", textDecoration: "none" }}>시장 대시보드</Link>
        <Link to="/stocks/kr/005930" style={{ color: "var(--fg-1)", textDecoration: "none" }}>종목 상세 리서치 예시</Link>
      </div>
    </Card>
  );
}

export function DesktopInsightsPage() {
  const marketParity = useMarketParity();
  const disparity = useCommonPreferredDisparity();

  return (
    <DesktopShell
      center={
        <div style={{ padding: 24, display: "grid", gap: 16 }}>
          <ReadOnlyGuardrailNote />
          <DecisionCard />

          <Card>
            <MarketParityStrip state={marketParity.state} reload={marketParity.reload} />
          </Card>

          {disparity.status === "loading" && <SectionStatus>보통주/우선주 괴리 데이터를 불러오는 중…</SectionStatus>}
          {disparity.status === "error" && <SectionStatus>보통주/우선주 괴리 데이터를 일시적으로 불러오지 못했습니다.</SectionStatus>}
          {disparity.status === "ready" && <CommonPreferredDisparityCardView data={disparity.data} />}

          <ForecastCalibrationPanel />
          <AnalysisArtifactPanel />
          <SessionContextTimelinePanel />

          <RelatedScreensCard />
        </div>
      }
    />
  );
}

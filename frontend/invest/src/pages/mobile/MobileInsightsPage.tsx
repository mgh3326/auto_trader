// /invest/insights (mobile) — read-only market insight cards.
// Mirrors DesktopInsightsPage.tsx's panel set, section order, and page-local
// coordination state (ROB-681). The small non-exported helpers and the
// empty/crosslink coordination state below are duplicated rather than shared
// (see docs/plans/ROB-681-mobile-insights-viewport-dispatch.md); ROB-682
// re-keyed the crosslink state on both this file and DesktopInsightsPage.tsx
// in lockstep (symbol key instead of correlation_id) to keep the duplicate
// copies from drifting apart.
import { useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { CommonPreferredDisparityCardView } from "../../components/CommonPreferredDisparityCard";
import { MarketParityStrip } from "../../components/home/MarketParityStrip";
import { AnalysisArtifactPanel } from "../../components/insights/AnalysisArtifactPanel";
import { ForecastCalibrationPanel } from "../../components/insights/ForecastCalibrationPanel";
import { SessionContextTimelinePanel } from "../../components/insights/SessionContextTimelinePanel";
import { RetrospectivesPanel } from "../../components/my/RetrospectivesPanel";
import { PageSafetyNote } from "../../components/PageSafetyNote";
import { MobileShell } from "../../mobile/MobileShell";
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

function PageHeader() {
  return (
    <div style={{ display: "grid", gap: 6 }}>
      <h1 style={{ margin: 0, fontSize: 22, letterSpacing: "-0.03em" }}>인사이트</h1>
      <p style={{ margin: 0, color: "var(--fg-2)", fontSize: 13, lineHeight: 1.6 }}>
        괴리·패리티 같은 시장 관찰과 예측 판단 품질·세션 기록을 한곳에서 봅니다. 모두 읽기 전용 관찰 자료입니다.
      </p>
    </div>
  );
}

// Lightweight section label — deliberately smaller/muted than the panels' own
// h2 titles so grouping reads as a divider, not a competing heading.
function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section style={{ display: "grid", gap: 10 }}>
      <h2
        style={{
          margin: 0,
          fontSize: 12,
          fontWeight: 800,
          color: "var(--fg-3)",
          letterSpacing: "0.04em",
        }}
      >
        {title}
      </h2>
      {children}
    </section>
  );
}

function AccumulatingBanner() {
  return (
    <div
      role="status"
      style={{
        padding: "12px 14px",
        background: "var(--surface-2)",
        border: "1px solid var(--border)",
        borderRadius: 12,
        color: "var(--fg-2)",
        fontSize: 13,
        lineHeight: 1.6,
      }}
    >
      판단 품질·핸드오프 데이터는 아직 축적 중입니다 — 예측 채점(forecast_resolve)·분석 아티팩트·세션
      메모가 쌓이면 아래 카드가 채워집니다.
    </div>
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

export function MobileInsightsPage() {
  const marketParity = useMarketParity();
  const disparity = useCommonPreferredDisparity();

  // Emptiness is owned by each panel (self-fetch); they report up via
  // onEmptyChange so the page can show one accumulating banner instead of a
  // stack of empty boxes. null = not-yet-resolved.
  const [forecastEmpty, setForecastEmpty] = useState<boolean | null>(null);
  const [artifactEmpty, setArtifactEmpty] = useState<boolean | null>(null);
  const [sessionEmpty, setSessionEmpty] = useState<boolean | null>(null);
  const allDataEmpty =
    forecastEmpty === true && artifactEmpty === true && sessionEmpty === true;

  // Crosslink closed forecasts ↔ retrospectives by normalized symbol key
  // (ROB-682): only the intersection (keys present on both sides) gets
  // anchors + links. Re-keyed from correlation_id (ROB-678), which was
  // structurally dead — the forecast/retro id namespaces never overlap.
  // Mirrors DesktopInsightsPage.tsx's coordination state 1:1 (see file-header
  // note on why this is duplicated rather than shared).
  const [closedForecastKeys, setClosedForecastKeys] = useState<string[]>([]);
  const [retroKeys, setRetroKeys] = useState<string[]>([]);
  const linkedSymbolKeys = useMemo(() => {
    const retro = new Set(retroKeys);
    return new Set(closedForecastKeys.filter((key) => retro.has(key)));
  }, [closedForecastKeys, retroKeys]);

  return (
    <MobileShell title="인사이트">
      <div style={{ display: "flex", flexDirection: "column", gap: 14, padding: "14px 0 16px" }}>
        <div style={{ padding: "0 16px" }}>
          <PageHeader />
        </div>

        {allDataEmpty && (
          <div style={{ padding: "0 16px" }}>
            <AccumulatingBanner />
          </div>
        )}

        <div style={{ padding: "0 16px" }}>
          <Section title="시장 관찰">
            <Card>
              <MarketParityStrip state={marketParity.state} reload={marketParity.reload} />
            </Card>
            {disparity.status === "loading" && <SectionStatus>보통주/우선주 괴리 데이터를 불러오는 중…</SectionStatus>}
            {disparity.status === "error" && <SectionStatus>보통주/우선주 괴리 데이터를 일시적으로 불러오지 못했습니다.</SectionStatus>}
            {disparity.status === "ready" && <CommonPreferredDisparityCardView data={disparity.data} />}
          </Section>
        </div>

        <div style={{ padding: "0 16px" }}>
          <Section title="판단 품질">
            <ForecastCalibrationPanel
              onEmptyChange={setForecastEmpty}
              onClosedSymbolKeys={setClosedForecastKeys}
              linkedSymbolKeys={linkedSymbolKeys}
            />
          </Section>
        </div>

        <div style={{ padding: "0 16px" }}>
          <Section title="학습·회고">
            <RetrospectivesPanel
              compact
              onSymbolKeys={setRetroKeys}
              linkedSymbolKeys={linkedSymbolKeys}
            />
          </Section>
        </div>

        <div style={{ padding: "0 16px" }}>
          <Section title="세션 기록">
            <AnalysisArtifactPanel onEmptyChange={setArtifactEmpty} />
            <SessionContextTimelinePanel onEmptyChange={setSessionEmpty} />
          </Section>
        </div>

        <div style={{ padding: "0 16px" }}>
          <RelatedScreensCard />
        </div>

        <div style={{ padding: "0 16px" }}>
          <ReadOnlyGuardrailNote />
        </div>
      </div>
    </MobileShell>
  );
}

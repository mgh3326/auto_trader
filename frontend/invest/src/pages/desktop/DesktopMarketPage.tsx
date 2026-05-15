import { Link } from "react-router-dom";
import { CommonPreferredDisparityCardView } from "../../components/CommonPreferredDisparityCard";
import { DesktopShell } from "../../desktop/DesktopShell";
import { Card } from "../../ds";
import { useCommonPreferredDisparity } from "../../hooks/useCommonPreferredDisparity";
import { useMarketDashboard } from "../../hooks/useMarketDashboard";
import type { MarketDashboardMetric, MarketDashboardSection, MarketDashboardState, MarketDashboardTone } from "../../types/marketDashboard";

const STATE_LABEL: Record<MarketDashboardState, string> = {
  fresh: "정상",
  partial: "부분",
  missing: "없음",
  error: "오류",
};

const STATE_COLOR: Record<MarketDashboardState, string> = {
  fresh: "#16a34a",
  partial: "#ca8a04",
  missing: "#dc2626",
  error: "#b91c1c",
};

const TONE_COLOR: Record<MarketDashboardTone, string> = {
  up: "var(--gain)",
  down: "var(--loss)",
  flat: "var(--flat)",
  unknown: "var(--fg-3)",
};

const TONE_ARROW: Record<MarketDashboardTone, string> = {
  up: "▲",
  down: "▼",
  flat: "·",
  unknown: "—",
};

function StatePill({ state }: { state: MarketDashboardState }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        borderRadius: 999,
        padding: "3px 8px",
        fontSize: 12,
        fontWeight: 800,
        color: "white",
        background: STATE_COLOR[state],
        whiteSpace: "nowrap",
      }}
    >
      {STATE_LABEL[state]}
    </span>
  );
}

function formatDelta(metric: MarketDashboardMetric) {
  const parts: string[] = [];
  if (metric.change != null) parts.push(`${metric.change >= 0 ? "+" : ""}${metric.change.toLocaleString()}`);
  if (metric.changePct != null) parts.push(`${metric.changePct >= 0 ? "+" : ""}${metric.changePct.toFixed(2)}%`);
  return parts.join(" · ") || "변동 정보 없음";
}

function MetricCard({ metric }: { metric: MarketDashboardMetric }) {
  const color = TONE_COLOR[metric.tone];
  return (
    <div
      style={{
        border: "1px solid var(--divider)",
        borderRadius: 16,
        padding: 14,
        display: "grid",
        gap: 8,
        minHeight: 132,
        background: metric.stale ? "var(--surface-2)" : "var(--surface)",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "start" }}>
        <div>
          <div style={{ fontSize: 13, color: "var(--fg-2)", fontWeight: 800 }}>{metric.label}</div>
          {metric.symbol && <div style={{ fontSize: 11, color: "var(--fg-3)", marginTop: 2 }}>{metric.symbol}</div>}
        </div>
        {metric.stale && <span style={{ color: "var(--warn)", fontSize: 11, fontWeight: 800 }}>stale</span>}
      </div>
      <div style={{ fontSize: 24, fontWeight: 900, letterSpacing: "-0.03em", fontFeatureSettings: '"tnum"' }}>
        {metric.value ?? "—"}{metric.unit ? <span style={{ fontSize: 13, marginLeft: 3, color: "var(--fg-3)" }}>{metric.unit}</span> : null}
      </div>
      <div style={{ color, fontWeight: 800, fontSize: 13, fontFeatureSettings: '"tnum"' }}>
        <span style={{ marginRight: 5 }}>{TONE_ARROW[metric.tone]}</span>{formatDelta(metric)}
      </div>
      <div style={{ color: "var(--fg-3)", fontSize: 11 }}>{metric.source}</div>
      {metric.warning && <div style={{ color: "var(--warn)", fontSize: 12 }}>⚠ {metric.warning}</div>}
    </div>
  );
}

function SectionCard({ section }: { section: MarketDashboardSection }) {
  return (
    <Card>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "start", marginBottom: 14 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 20, letterSpacing: "-0.03em" }}>{section.title}</h2>
          <p style={{ margin: "5px 0 0", color: "var(--fg-2)", fontSize: 13 }}>{section.subtitle}</p>
          <div style={{ marginTop: 6, color: "var(--fg-3)", fontSize: 12 }}>
            기준: {section.reference} · {section.sourceOfTruth}{section.updatedAt ? ` · ${section.updatedAt}` : ""}
          </div>
        </div>
        <StatePill state={section.state} />
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))", gap: 12 }}>
        {section.metrics.map((metric) => <MetricCard key={`${section.id}-${metric.label}-${metric.symbol ?? ""}`} metric={metric} />)}
      </div>
      {(section.warnings.length > 0 || section.notes.length > 0) && (
        <div style={{ marginTop: 12, display: "grid", gap: 4, color: "var(--fg-3)", fontSize: 12 }}>
          {section.warnings.map((warning) => <div key={warning} style={{ color: "var(--warn)" }}>⚠ {warning}</div>)}
          {section.notes.map((note) => <div key={note}>• {note}</div>)}
        </div>
      )}
    </Card>
  );
}

export function DesktopMarketPage() {
  const { state, reload } = useMarketDashboard();
  const disparityState = useCommonPreferredDisparity();
  const data = state.status === "ready" ? state.data : null;

  return (
    <DesktopShell
      center={
        <div style={{ padding: 24, display: "grid", gap: 16 }}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "start", flexWrap: "wrap" }}>
            <div>
              <h1 style={{ margin: 0, fontSize: 28, letterSpacing: "-0.05em" }}>시장</h1>
              <p style={{ margin: "6px 0 0", color: "var(--fg-2)", fontSize: 14 }}>
                국내 지수, 글로벌 지수, 환율/매크로, crypto read-only 스냅샷입니다.
              </p>
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <Link
                to="/insights"
                style={{ padding: "8px 12px", borderRadius: 10, border: "1px solid var(--border)", background: "var(--surface)", color: "var(--fg-1)", fontWeight: 800, textDecoration: "none" }}
              >
                인사이트
              </Link>
              <Link
                to="/market/fx"
                style={{ padding: "8px 12px", borderRadius: 10, border: "1px solid var(--border)", background: "var(--surface)", color: "var(--fg-1)", fontWeight: 800, textDecoration: "none" }}
              >
                FX·매크로 상세
              </Link>
              <button
                type="button"
                onClick={reload}
                style={{ padding: "8px 12px", borderRadius: 10, border: "1px solid var(--border)", background: "var(--surface)", color: "var(--fg-1)", fontWeight: 800, cursor: "pointer" }}
              >
                새로고침
              </button>
            </div>
          </div>

          {state.status === "loading" && <Card>시장 데이터를 불러오는 중…</Card>}
          {state.status === "error" && <Card><span style={{ color: "var(--danger)" }}>시장 데이터를 일시적으로 불러오지 못했습니다.</span></Card>}

          {data && (
            <>
              <Card>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
                  <div>
                    <div style={{ fontWeight: 900 }}>시장 대시보드 상태</div>
                    <div style={{ color: "var(--fg-3)", fontSize: 12, marginTop: 4 }}>asOf {data.asOf}</div>
                  </div>
                  <StatePill state={data.state} />
                </div>
                {data.warnings.length > 0 && (
                  <div style={{ marginTop: 10, color: "var(--warn)", fontSize: 12 }}>{data.warnings.map((w) => `⚠ ${w}`).join(" · ")}</div>
                )}
              </Card>
              {disparityState.status === "loading" && <Card>보통주/우선주 괴리 데이터를 불러오는 중…</Card>}
              {disparityState.status === "error" && <Card><span style={{ color: "var(--danger)" }}>보통주/우선주 괴리 데이터를 일시적으로 불러오지 못했습니다.</span></Card>}
              {disparityState.status === "ready" && <CommonPreferredDisparityCardView data={disparityState.data} />}
              {data.sections.map((section) => <SectionCard key={section.id} section={section} />)}
            </>
          )}
        </div>
      }
      right={
        <Card>
          <div style={{ fontWeight: 900, marginBottom: 8 }}>읽기 전용 원칙</div>
          <ul style={{ margin: 0, paddingLeft: 18, color: "var(--fg-2)", fontSize: 13, lineHeight: 1.7 }}>
            <li>주문·매매 API를 호출하지 않습니다.</li>
            <li>시장/지수 provider 응답을 표시만 합니다.</li>
            <li><Link to="/coverage" style={{ color: "inherit" }}>커버리지</Link>에서 freshness를 별도 확인합니다.</li>
          </ul>
        </Card>
      }
    />
  );
}

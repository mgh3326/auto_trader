import { Link } from "react-router-dom";
import { Card } from "../../ds";
import type {
  DefenseSignalConfidence,
  FxDashboardAfterVerification,
  FxDashboardCollectionItem,
  FxDashboardDataState,
  FxDashboardDefenseSignal,
  FxDashboardDisclaimer,
  FxDashboardEventsSection,
  FxDashboardForeignFlowSection,
  FxDashboardQuoteMetric,
  FxDashboardResponse,
  FxDashboardSourceFreshness,
  FxDashboardThreshold,
  FxDashboardTone,
} from "../../types/fxDashboard";

const STATE_LABEL: Record<FxDashboardDataState, string> = {
  fresh: "정상",
  partial: "부분",
  missing: "없음",
  stale: "stale",
  error: "오류",
};

const STATE_COLOR: Record<FxDashboardDataState, string> = {
  fresh: "#16a34a",
  partial: "#ca8a04",
  missing: "#dc2626",
  stale: "#8b5cf6",
  error: "#b91c1c",
};

const TONE_COLOR: Record<FxDashboardTone, string> = {
  up: "var(--gain)",
  down: "var(--loss)",
  flat: "var(--flat)",
  unknown: "var(--fg-3)",
};

const TONE_ARROW: Record<FxDashboardTone, string> = {
  up: "▲",
  down: "▼",
  flat: "·",
  unknown: "—",
};

const CONFIDENCE_LABEL: Record<DefenseSignalConfidence, string> = {
  low: "낮음",
  medium: "중간",
  high: "높음",
};

function formatNumber(value?: number | null, digits = 2) {
  if (value == null || Number.isNaN(value)) return "—";
  return value.toLocaleString("ko-KR", { maximumFractionDigits: digits });
}

function formatSigned(value?: number | null, suffix = "") {
  if (value == null || Number.isNaN(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toLocaleString("ko-KR", { maximumFractionDigits: 2 })}${suffix}`;
}

function formatDate(value?: string | null) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("ko-KR", { dateStyle: "short", timeStyle: "short" });
}

export function FxStatePill({ state }: { state: FxDashboardDataState }) {
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

export function FxToneValue({ value, changePct, tone }: { value?: number | null; changePct?: number | null; tone: FxDashboardTone }) {
  const color = TONE_COLOR[tone];
  return (
    <div style={{ display: "grid", gap: 5 }}>
      <div style={{ fontSize: 28, fontWeight: 900, letterSpacing: "-0.04em", fontFeatureSettings: '"tnum"' }}>
        {formatNumber(value ?? null)}
      </div>
      <div style={{ color, fontWeight: 800, fontSize: 13, fontFeatureSettings: '"tnum"' }}>
        <span style={{ marginRight: 5 }}>{TONE_ARROW[tone]}</span>
        {formatSigned(changePct, "%")}
      </div>
    </div>
  );
}

export function FxSourceFreshnessList({ sources }: { sources: FxDashboardSourceFreshness[] }) {
  return (
    <Card>
      <h2 style={{ margin: 0, fontSize: 19, letterSpacing: "-0.03em" }}>소스 freshness</h2>
      <div style={{ display: "grid", gap: 10, marginTop: 14 }}>
        {sources.map((source) => (
          <div key={source.source} style={{ display: "flex", gap: 12, justifyContent: "space-between", alignItems: "start", borderBottom: "1px solid var(--divider)", paddingBottom: 10 }}>
            <div>
              <div style={{ fontWeight: 900 }}>{source.label}</div>
              <div style={{ color: "var(--fg-3)", fontSize: 12, marginTop: 3 }}>
                {source.source} · 갱신 {formatDate(source.updatedAt)}
                {source.staleAfterMinutes != null ? ` · stale 기준 ${source.staleAfterMinutes}분` : ""}
              </div>
              {source.warning && <div style={{ color: "var(--warn)", fontSize: 12, marginTop: 4 }}>⚠ {source.warning}</div>}
            </div>
            <FxStatePill state={source.dataState} />
          </div>
        ))}
      </div>
    </Card>
  );
}

export function FxQuoteCard({ metric }: { metric: FxDashboardQuoteMetric }) {
  return (
    <Card>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "start" }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 19, letterSpacing: "-0.03em" }}>{metric.label ?? metric.symbol}</h2>
          <div style={{ color: "var(--fg-3)", fontSize: 12, marginTop: 4 }}>
            {metric.symbol} · {metric.source} · {formatDate(metric.updatedAt)}
          </div>
        </div>
        {metric.dataState && <FxStatePill state={metric.dataState} />}
      </div>
      <div style={{ marginTop: 14 }}>
        <FxToneValue value={metric.value ?? metric.spot} changePct={metric.changePct} tone={metric.tone} />
        {metric.change != null && <div style={{ color: "var(--fg-3)", fontSize: 12, marginTop: 5 }}>변동폭 {formatSigned(metric.change)}</div>}
      </div>
    </Card>
  );
}

export function FxThresholdCard({ thresholds }: { thresholds: FxDashboardThreshold[] }) {
  return (
    <Card>
      <h2 style={{ margin: 0, fontSize: 19, letterSpacing: "-0.03em" }}>USD/KRW 경계값</h2>
      <div style={{ display: "grid", gap: 10, marginTop: 14 }}>
        {thresholds.map((threshold) => (
          <div key={`${threshold.level}-${threshold.label}`} style={{ display: "flex", justifyContent: "space-between", gap: 12, border: "1px solid var(--divider)", borderRadius: 12, padding: 12 }}>
            <div>
              <div style={{ fontWeight: 900 }}>{threshold.label}</div>
              <div style={{ color: "var(--fg-3)", fontSize: 12, marginTop: 3 }}>{threshold.level.toLocaleString("ko-KR")}</div>
            </div>
            <div style={{ textAlign: "right", fontSize: 12, color: "var(--fg-2)", fontWeight: 800 }}>
              <div>{formatSigned(threshold.distancePct, "%")}</div>
              <div style={{ color: "var(--fg-3)", marginTop: 3 }}>{threshold.state}</div>
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

export function FxDefenseSignalCard({ signal, disclaimers }: { signal: FxDashboardDefenseSignal; disclaimers: FxDashboardDisclaimer[] }) {
  return (
    <Card>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "start", flexWrap: "wrap" }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 19, letterSpacing: "-0.03em" }}>{signal.labelKo}</h2>
          <p style={{ margin: "6px 0 0", color: "var(--fg-2)", fontSize: 13 }}>{signal.summaryKo}</p>
        </div>
        <div style={{ textAlign: "right", fontSize: 12, color: "var(--fg-3)" }}>
          <div style={{ fontSize: 22, fontWeight: 900, color: "var(--fg-1)" }}>{signal.score}</div>
          <div>신뢰도 {CONFIDENCE_LABEL[signal.confidence]}</div>
        </div>
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 12 }}>
        {signal.notConfirmedIntervention && <span style={badgeStyle}>개입 미확정</span>}
        {signal.needsAfterVerification && <span style={badgeStyle}>사후 검증 필요</span>}
        <span style={badgeStyle}>읽기 전용</span>
      </div>
      {signal.reasonsKo.length > 0 && (
        <ul style={{ margin: "14px 0 0", paddingLeft: 18, color: "var(--fg-2)", fontSize: 13, lineHeight: 1.65 }}>
          {signal.reasonsKo.map((reason) => <li key={reason}>{reason}</li>)}
        </ul>
      )}
      {signal.evidence.length > 0 && (
        <div style={{ display: "grid", gap: 8, marginTop: 14 }}>
          {signal.evidence.map((item) => (
            <div key={`${item.kind}-${item.labelKo}`} style={{ display: "flex", justifyContent: "space-between", gap: 10, fontSize: 12, color: "var(--fg-2)" }}>
              <span>{item.labelKo}{item.value ? ` · ${item.value}` : ""}</span>
              <FxStatePill state={item.dataState} />
            </div>
          ))}
        </div>
      )}
      {disclaimers.length > 0 && (
        <div style={{ display: "grid", gap: 6, marginTop: 14, color: "var(--warn)", fontSize: 12 }}>
          {disclaimers.map((item) => <div key={item.code}>⚠ {item.textKo}</div>)}
        </div>
      )}
    </Card>
  );
}

export function FxCollectionCard({ title, items }: { title: string; items: FxDashboardCollectionItem[] }) {
  return (
    <Card>
      <h2 style={{ margin: 0, fontSize: 19, letterSpacing: "-0.03em" }}>{title}</h2>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 10, marginTop: 14 }}>
        {items.map((item) => (
          <div key={`${title}-${item.symbol}`} style={{ border: "1px solid var(--divider)", borderRadius: 12, padding: 12, display: "grid", gap: 6 }}>
            <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
              <div>
                <div style={{ fontWeight: 900 }}>{item.label}</div>
                <div style={{ color: "var(--fg-3)", fontSize: 11 }}>{item.symbol} · {item.source}</div>
              </div>
              <FxStatePill state={item.dataState} />
            </div>
            <div style={{ fontSize: 20, fontWeight: 900 }}>{formatNumber(item.value)}</div>
            <div style={{ color: item.changePct != null && item.changePct < 0 ? "var(--loss)" : "var(--gain)", fontSize: 12, fontWeight: 800 }}>
              {formatSigned(item.changePct, "%")}
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

export function FxDeferredSectionsCard({ foreignFlow, news, events, afterVerification }: {
  foreignFlow: FxDashboardForeignFlowSection;
  news: { dataState: FxDashboardDataState; warning?: string | null; items: { title: string; source: string; publishedAt?: string | null }[] };
  events: FxDashboardEventsSection;
  afterVerification: FxDashboardAfterVerification;
}) {
  const rows = [
    { label: "외국인 수급", state: foreignFlow.dataState, text: foreignFlow.summaryKo },
    { label: "뉴스", state: news.dataState, text: news.warning ?? (news.items.length > 0 ? news.items.map((item) => item.title).join(" · ") : "관련 뉴스가 아직 충분하지 않습니다.") },
    { label: "일정", state: events.dataState, text: events.warning ?? (events.items.length > 0 ? events.items.map((item) => item.title).join(" · ") : "관련 일정이 아직 충분하지 않습니다.") },
    { label: "사후 검증", state: afterVerification.dataState, text: afterVerification.summaryKo },
  ];
  return (
    <Card>
      <h2 style={{ margin: 0, fontSize: 19, letterSpacing: "-0.03em" }}>부분/사후 검증 상태</h2>
      <div style={{ display: "grid", gap: 10, marginTop: 14 }}>
        {rows.map((row) => (
          <div key={row.label} style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "start", border: "1px solid var(--divider)", borderRadius: 12, padding: 12 }}>
            <div>
              <div style={{ fontWeight: 900 }}>{row.label}</div>
              <div style={{ color: "var(--fg-2)", fontSize: 12, marginTop: 4 }}>{row.text}</div>
            </div>
            <FxStatePill state={row.state} />
          </div>
        ))}
      </div>
    </Card>
  );
}

export function FxMiniCard({ data }: { data: FxDashboardResponse }) {
  return (
    <Link to="/market/fx" style={{ textDecoration: "none", color: "inherit" }}>
      <Card style={{ display: "grid", gap: 10 }}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 10, alignItems: "start" }}>
          <div>
            <div style={{ fontWeight: 900 }}>FX·매크로 경고</div>
            <div style={{ color: "var(--fg-3)", fontSize: 12, marginTop: 3 }}>참고용 · 매매/주문 없음</div>
          </div>
          <FxStatePill state={data.dataState} />
        </div>
        <FxToneValue value={data.usdKrw.value ?? data.usdKrw.spot} changePct={data.usdKrw.changePct} tone={data.usdKrw.tone} />
        <div style={{ color: "var(--fg-2)", fontSize: 12 }}>{data.defenseSignal.summaryKo}</div>
        <div style={{ color: "var(--accent)", fontWeight: 900, fontSize: 12 }}>상세 보기 →</div>
      </Card>
    </Link>
  );
}

const badgeStyle = {
  borderRadius: 999,
  padding: "4px 8px",
  border: "1px solid var(--border)",
  background: "var(--surface-2)",
  color: "var(--fg-2)",
  fontSize: 12,
  fontWeight: 800,
};

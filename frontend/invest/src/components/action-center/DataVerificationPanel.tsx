import { Card } from "../../ds";
import type { AnalysisReport, AnalysisStageResult } from "../../types/actionCenter";
import { StatusBadge } from "./StatusBadge";

const UNAVAILABLE = "확인 불가";

const KEY_LABELS: Record<string, string> = {
  mcp: "MCP 수집",
  toss: "Toss 교차 확인",
  naver: "Naver 교차 확인",
  as_of_kst: "기준 시각",
  invest_page: "투자 화면",
  accounts: "계좌/보유",
  symbols_analyzed: "분석 종목",
  external_cross_checks: "외부 교차 검증",
  not_checked_or_unavailable: "추가 확인 필요",
  accountFeasibility: "계좌 검증",
  marketLiquidity: "시장/유동성",
  eventRisk: "이벤트 리스크",
};

const STAGE_LABELS: Record<string, string> = {
  account_feasibility: "계좌 검증",
  market_liquidity: "시장/유동성",
  event_risk: "이벤트 리스크",
  candidate_generation: "후보 생성",
};

const SOURCE_LABELS: Record<string, string> = {
  kis_live: "KIS 실계좌",
  upbit: "Upbit",
  mcp: "MCP",
  naver: "Naver",
  toss: "Toss",
};

function labelText(key: string): string {
  return KEY_LABELS[key] ?? key;
}

function stageText(key: string): string {
  return STAGE_LABELS[key] ?? key;
}

function sourceText(source: string): string {
  return SOURCE_LABELS[source] ?? source;
}

function normalizeText(value: unknown): string {
  if (value == null || value === "") return UNAVAILABLE;
  if (Array.isArray(value)) return value.length === 0 ? UNAVAILABLE : value.map(normalizeText).join(" · ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value)
    .replace(/^확인 불가[:：]?\s*/i, "추가 확인 필요: ")
    .replace(/tab detected but detailed values not extracted/gi, "탭은 확인했지만 상세 값은 추출하지 못함")
    .replace(/stock\.naver\.com\/market\/crypto checked/gi, "Naver 코인 시장 페이지 확인")
    .replace(/visible; layout issue identified in long text\/grid cards/gi, "접근 가능 · 긴 문구/그리드 카드 레이아웃 문제 확인")
    .replace(/cash\/holdings\/pending\/journals collected around/gi, "현금·보유·대기 주문·저널 수집")
    .replace(/Toss detailed page values/gi, "Toss 상세 페이지 값")
    .replace(/Upbit staking lock\/sellable quantity details beyond holdings split/gi, "Upbit 스테이킹 잠금·매도 가능 수량 상세")
    .replace(/Naver crypto market page/gi, "Naver 코인 시장 페이지")
    .replace(/Chrome remote_debug tab inventory/gi, "Chrome 원격 디버그 탭 목록");
}

function valueText(value: unknown): string {
  return normalizeText(value);
}

function KeyValueGrid({ values }: { values: Record<string, unknown> }) {
  const entries = Object.entries(values);
  if (entries.length === 0) {
    return <div style={{ color: "var(--fg-3)", fontSize: 12 }}>표시할 항목이 없습니다.</div>;
  }
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 8 }}>
      {entries.map(([key, value]) => {
        const text = valueText(value);
        return (
          <div key={key} style={{ padding: 10, borderRadius: 12, background: "var(--surface-2)", display: "grid", gap: 3, minWidth: 0 }}>
            <div style={{ color: "var(--fg-3)", fontSize: 11, overflowWrap: "anywhere" }}>{labelText(key)}</div>
            <div
              style={{
                color: text === UNAVAILABLE ? "var(--warn)" : "var(--fg-1)",
                fontWeight: 800,
                fontSize: 12,
                lineHeight: 1.45,
                maxHeight: 96,
                overflow: "auto",
                overflowWrap: "anywhere",
                whiteSpace: "pre-wrap",
              }}
            >
              {text}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function StageRow({ stage }: { stage: AnalysisStageResult }) {
  return (
    <div style={{ padding: "9px 0", borderTop: "1px solid var(--divider)", display: "grid", gap: 4 }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "center" }}>
        <strong style={{ fontSize: 13 }}>{stageText(stage.stageKey)}</strong>
        <StatusBadge status={stage.status} />
      </div>
      <div style={{ color: "var(--fg-3)", fontSize: 12 }}>
        {sourceText(stage.source)} · {stage.freshnessAt ?? normalizeText(stage.unavailableReason) ?? UNAVAILABLE}
      </div>
      {stage.warnings && stage.warnings.length > 0 && (
        <div style={{ color: "var(--warn)", fontSize: 12 }}>{stage.warnings.map(normalizeText).join(" · ")}</div>
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
            MCP 계좌·주문 데이터가 권위 기준입니다. Toss/Naver는 교차 검증 참고이며, 확인되지 않은 핵심 값은 {UNAVAILABLE}로 표시합니다.
          </div>
        </div>
        <div>
          <div style={{ color: "var(--fg-3)", fontSize: 12, fontWeight: 800, marginBottom: 6 }}>데이터 신선도</div>
          <KeyValueGrid values={report.dataFreshness ?? {}} />
        </div>
        <div>
          <div style={{ color: "var(--fg-3)", fontSize: 12, fontWeight: 800, marginBottom: 6 }}>검증 범위</div>
          <KeyValueGrid values={report.coverage ?? {}} />
        </div>
        {report.stageResults && report.stageResults.length > 0 && (
          <div>
            <div style={{ color: "var(--fg-3)", fontSize: 12, fontWeight: 800, marginBottom: 2 }}>단계별 점검</div>
            {report.stageResults.map((stage) => <StageRow key={`${stage.stageKey}:${stage.source}`} stage={stage} />)}
          </div>
        )}
      </div>
    </Card>
  );
}

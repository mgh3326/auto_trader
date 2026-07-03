// ROB-265 Plan 5 — `/invest/reports/:reportUuid` detail view.
//
// Surfaces the Plan 4 delivery_status on each watch event so operators
// can see whether the Hermes notification actually reached them.

import { useEffect, type ReactNode } from "react";
import { Link, useLocation, useParams } from "react-router-dom";

import { Card, Pill } from "../../ds";
import { useInvestmentReportBundle } from "../../hooks/useInvestmentReportBundle";
import { LinkedOrderRow, linkedOrderKey } from "../orders/LinkedOrderRow";
import type {
  DeliveryStatus,
  InvestmentReportItem,
  InvestmentReportItemDecision,
  InvestmentWatchAlert,
  InvestmentWatchEvent,
  NoActionSummary,
  ReportReviewSections,
  SnapshotFreshnessSummary,
  SnapshotReportDiagnostics,
} from "../../types/investmentReports";
import { ActionPacketView } from "./ActionPacketView";
import { IntermediateAnalysisPanel } from "./IntermediateAnalysisPanel";
import { ProposalDiffPanel } from "./ProposalDiffPanel";
import { ReportDiagnosticsPanel } from "./ReportDiagnosticsPanel";
import { ReportSnapshotEvidencePanel } from "./ReportSnapshotEvidencePanel";
import { SnapshotBundleFreshnessChip } from "./SnapshotBundleFreshnessChip";

// ROB-274 — primary category badges switched to English (locked decision §4
// minor #5). Korean explanatory body copy (e.g. "리스크" / "무액션 노트"
// in this file's report header) is preserved.
const ITEM_KIND_LABELS: Record<string, string> = {
  action: "action",
  watch: "watch",
  risk: "risk",
};

const ITEM_STATUS_LABELS: Record<string, string> = {
  proposed: "검토 대기",
  approved: "승인",
  denied: "거절",
  deferred: "보류",
  activated: "활성화됨",
  expired: "만료",
};

const DECISION_LABELS: Record<string, string> = {
  approve: "승인",
  deny: "거절",
  defer: "보류",
  skip: "건너뜀",
  partial_approve: "부분 승인",
};

const ALERT_STATUS_LABELS: Record<string, string> = {
  active: "활성",
  triggered: "발화됨",
  expired: "만료",
  canceled: "취소",
};

const DELIVERY_STATUS_LABELS: Record<DeliveryStatus, string> = {
  pending: "전송 대기",
  delivered: "전송 완료",
  skipped: "전송 건너뜀",
  failed: "전송 실패",
};

const DELIVERY_STATUS_COLORS: Record<DeliveryStatus, string> = {
  pending: "var(--fg-3)",
  delivered: "var(--success, var(--fg-2))",
  skipped: "var(--warn)",
  failed: "var(--danger)",
};

function StatusCard({ children }: { children: ReactNode }) {
  return (
    <Card>
      <div role="status" style={{ color: "var(--fg-3)", fontSize: 13 }}>
        {children}
      </div>
    </Card>
  );
}

function ReportHeader({
  title,
  market,
  marketSession,
  accountScope,
  executionMode,
  status,
  summary,
  riskSummary,
  thesisText,
  noActionNote,
  createdAt,
  freshnessSummary,
  reportDiagnostics,
}: {
  title: string;
  market: string;
  marketSession?: string | null;
  accountScope?: string | null;
  executionMode: string;
  status: string;
  summary: string;
  riskSummary?: string | null;
  thesisText?: string | null;
  noActionNote?: string | null;
  createdAt: string;
  freshnessSummary?: SnapshotFreshnessSummary | null;
  reportDiagnostics?: SnapshotReportDiagnostics | null;
}) {
  return (
    <Card>
      <div style={{ display: "grid", gap: 10 }}>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "baseline",
            gap: 10,
            flexWrap: "wrap",
          }}
        >
          <h1
            style={{ margin: 0, fontSize: 24, letterSpacing: "-0.04em" }}
          >
            {title}
          </h1>
          <span style={{ color: "var(--fg-3)", fontSize: 12, fontWeight: 800 }}>
            {status}
          </span>
        </div>
        <div
          style={{
            color: "var(--fg-3)",
            fontSize: 12,
            display: "flex",
            gap: 8,
            flexWrap: "wrap",
          }}
        >
          <span>{market}</span>
          {marketSession ? (
            <>
              <span>·</span>
              <span>{marketSession}</span>
            </>
          ) : null}
          {accountScope ? (
            <>
              <span>·</span>
              <span>{accountScope}</span>
            </>
          ) : null}
          <span>·</span>
          <span>{executionMode}</span>
          <span>·</span>
          <span>{new Date(createdAt).toLocaleString("ko-KR")}</span>
        </div>
        {/* ROB-269 Phase 4 — bundle provenance chip. Returns null when the
            report has no snapshot metadata (legacy reports / Phase 3 flag
            off). Renders Korean-facing summary + degraded per-source chips. */}
        <SnapshotBundleFreshnessChip freshnessSummary={freshnessSummary ?? null} />
        {/* ROB-318 Phase 3 (PR-C) — deterministic report diagnostics. Returns
            null for legacy reports / when nothing degraded is worth showing. */}
        <ReportDiagnosticsPanel diagnostics={reportDiagnostics ?? null} />
        <p style={{ margin: 0, fontSize: 14, lineHeight: 1.65 }}>{summary}</p>
        {thesisText ? (
          <div style={{ color: "var(--fg-2)", fontSize: 13, lineHeight: 1.6 }}>
            <strong style={{ marginRight: 6 }}>논거</strong>
            {thesisText}
          </div>
        ) : null}
        {riskSummary ? (
          <div style={{ color: "var(--warn)", fontSize: 13, lineHeight: 1.6 }}>
            <strong style={{ marginRight: 6 }}>리스크</strong>
            {riskSummary}
          </div>
        ) : null}
        {noActionNote ? (
          <div style={{ color: "var(--fg-3)", fontSize: 13, lineHeight: 1.6 }}>
            <strong style={{ marginRight: 6 }}>무액션 노트</strong>
            {noActionNote}
          </div>
        ) : null}
      </div>
    </Card>
  );
}

// ROB-322 — confidence is a 0-100 Decimal serialised as string/number; show a
// rounded integer chip when present, otherwise nothing (never "확인 불가").
function formatConfidence(
  raw: number | string | null | undefined,
): string | null {
  if (raw === null || raw === undefined || raw === "") return null;
  const n = typeof raw === "number" ? raw : Number(raw);
  if (Number.isFinite(n)) return String(Math.round(n));
  return typeof raw === "string" ? raw : null;
}

// ROB-690 — server-computed risk/reward, read from
// evidenceSnapshot.trade_setup (migration-0 JSONB reserved key; see
// app/services/investment_reports/risk_reward.py). Fail-closed writes omit
// the key entirely, so its mere presence implies a valid computed setup —
// but we still defensively re-validate shape/finiteness before rendering,
// since evidenceSnapshot is untyped `Record<string, unknown>` on the wire.
interface TradeSetupHeadlineView {
  entry: string;
  riskPct: string;
  rewardPct: string;
  rrRatio: string;
}

interface TradeSetupView {
  direction: "long" | "short";
  headline: TradeSetupHeadlineView;
}

function parseTradeSetup(
  evidenceSnapshot: Record<string, unknown> | null | undefined,
): TradeSetupView | null {
  const raw = evidenceSnapshot?.trade_setup;
  if (!raw || typeof raw !== "object") return null;
  const setup = raw as Record<string, unknown>;

  const direction = setup.direction;
  if (direction !== "long" && direction !== "short") return null;

  const headlineRaw = setup.headline;
  if (!headlineRaw || typeof headlineRaw !== "object") return null;
  const headline = headlineRaw as Record<string, unknown>;

  const entry = headline.entry;
  const riskPct = headline.risk_pct;
  const rewardPct = headline.reward_pct;
  const rrRatio = headline.rr_ratio;
  if (
    typeof entry !== "string" ||
    typeof riskPct !== "string" ||
    typeof rewardPct !== "string" ||
    typeof rrRatio !== "string"
  ) {
    return null;
  }
  if (
    ![entry, riskPct, rewardPct, rrRatio].every((v) => Number.isFinite(Number(v)))
  ) {
    return null;
  }

  return { direction, headline: { entry, riskPct, rewardPct, rrRatio } };
}

function ItemRow({
  item,
  decisions,
}: {
  item: InvestmentReportItem;
  decisions: InvestmentReportItemDecision[];
}) {
  const kindLabel = ITEM_KIND_LABELS[item.itemKind] ?? item.itemKind;
  const statusLabel = ITEM_STATUS_LABELS[item.status] ?? item.status;
  const confidenceLabel = formatConfidence(item.confidence);
  const dimensionCitations = item.citedDimensionReportUuids?.length ?? 0;
  const hasChips =
    !!confidenceLabel || !!item.citedSymbolReportUuid || dimensionCitations > 0;
  const tradeSetup = parseTradeSetup(item.evidenceSnapshot);
  return (
    <section
      id={`watch-item-${item.itemUuid}`}
      style={{
        display: "grid",
        gap: 8,
        padding: 12,
        border: "1px solid var(--border)",
        borderRadius: 12,
        background: "rgba(255,255,255,0.015)",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          gap: 10,
          alignItems: "baseline",
          flexWrap: "wrap",
        }}
      >
        <div style={{ display: "grid", gap: 2 }}>
          <h3 style={{ margin: 0, fontSize: 15 }}>
            <span style={{ color: "var(--fg-3)", marginRight: 8 }}>
              {kindLabel}
            </span>
            {item.symbol ?? "—"}
            {item.side ? ` · ${item.side === "buy" ? "매수" : "매도"}` : ""}
          </h3>
          <div style={{ color: "var(--fg-3)", fontSize: 12 }}>
            {item.intent} · {item.targetKind} · priority {item.priority}
          </div>
        </div>
        <span style={{ fontSize: 12, fontWeight: 800 }}>{statusLabel}</span>
      </div>
      {hasChips ? (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {confidenceLabel ? (
            <Pill tone="accent" size="sm">
              신뢰도 {confidenceLabel}
            </Pill>
          ) : null}
          {item.citedSymbolReportUuid ? (
            <Pill tone="paper" size="sm">
              심볼 리포트
            </Pill>
          ) : null}
          {dimensionCitations > 0 ? (
            <Pill tone="paper" size="sm">
              차원 리포트 {dimensionCitations}
            </Pill>
          ) : null}
        </div>
      ) : null}
      <div style={{ color: "var(--fg-2)", fontSize: 13, lineHeight: 1.55 }}>
        {item.rationale}
      </div>
      {tradeSetup ? (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
          <Pill tone={tradeSetup.direction === "long" ? "gain" : "warn"} size="sm">
            {tradeSetup.direction === "long" ? "롱" : "숏"}
          </Pill>
          <span style={{ fontSize: 12, color: "var(--fg-2)" }}>
            손익비 R:R {tradeSetup.headline.rrRatio} · 리스크{" "}
            {tradeSetup.headline.riskPct}% · 리워드 {tradeSetup.headline.rewardPct}%
          </span>
        </div>
      ) : null}
      {item.linkedOrders && item.linkedOrders.length > 0 ? (
        <div style={{ display: "grid", gap: 6 }}>
          <div style={{ fontSize: 12, color: "var(--fg-2)", fontWeight: 800 }}>
            주문 · 체결
          </div>
          {item.linkedOrders.map((order) => (
            <LinkedOrderRow key={linkedOrderKey(order)} order={order} />
          ))}
        </div>
      ) : null}
      {item.watchCondition ? (
        <div
          style={{
            fontSize: 12,
            color: "var(--fg-3)",
            background: "var(--surface-2)",
            padding: "6px 10px",
            borderRadius: 8,
            fontFamily: "var(--mono, monospace)",
          }}
        >
          watch_condition: {JSON.stringify(item.watchCondition)}
        </div>
      ) : null}
      {item.operation && item.operation !== "create" ? (
        <ProposalDiffPanel
          operation={item.operation}
          targetRef={item.targetRef}
          currentState={item.currentState}
          proposedState={item.proposedState}
          diff={item.diff}
        />
      ) : null}
      {decisions.length > 0 ? (
        <div style={{ display: "grid", gap: 4 }}>
          <div
            style={{
              fontSize: 12,
              color: "var(--fg-2)",
              fontWeight: 800,
            }}
          >
            결정 이력
          </div>
          {decisions.map((decision) => (
            <div
              key={decision.decisionUuid}
              style={{
                fontSize: 12,
                color: "var(--fg-3)",
                display: "flex",
                gap: 8,
                flexWrap: "wrap",
              }}
            >
              <span>
                {DECISION_LABELS[decision.decision] ?? decision.decision}
              </span>
              <span>·</span>
              <span>{decision.actor}</span>
              <span>·</span>
              <span>{new Date(decision.createdAt).toLocaleString("ko-KR")}</span>
              {decision.decisionNote ? (
                <>
                  <span>·</span>
                  <span>{decision.decisionNote}</span>
                </>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function AlertRow({ alert }: { alert: InvestmentWatchAlert }) {
  return (
    <section
      id={`watch-alert-${alert.alertUuid}`}
      style={{
        display: "grid",
        gap: 6,
        padding: 10,
        border: "1px solid var(--border)",
        borderRadius: 10,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          gap: 10,
          alignItems: "baseline",
        }}
      >
        <strong style={{ fontSize: 14 }}>
          {alert.symbol} {alert.metric} {alert.operator} {alert.threshold}
        </strong>
        <span style={{ fontSize: 12, color: "var(--fg-3)" }}>
          {ALERT_STATUS_LABELS[alert.status] ?? alert.status}
        </span>
      </div>
      <div style={{ fontSize: 12, color: "var(--fg-3)" }}>
        {alert.intent} · {alert.actionMode} · 유효기간{" "}
        {new Date(alert.validUntil).toLocaleString("ko-KR")}
      </div>
    </section>
  );
}

function EventRow({ event }: { event: InvestmentWatchEvent }) {
  const deliveryLabel = DELIVERY_STATUS_LABELS[event.deliveryStatus];
  const deliveryColor = DELIVERY_STATUS_COLORS[event.deliveryStatus];
  return (
    <section
      id={`watch-event-${event.eventUuid}`}
      style={{
        display: "grid",
        gap: 6,
        padding: 10,
        border: "1px solid var(--border)",
        borderRadius: 10,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          gap: 10,
          alignItems: "baseline",
          flexWrap: "wrap",
        }}
      >
        <strong style={{ fontSize: 14 }}>
          {event.symbol} {event.metric} {event.operator} {event.threshold}
          {event.currentValue ? ` (현재 ${event.currentValue})` : ""}
        </strong>
        <span
          style={{
            fontSize: 12,
            fontWeight: 800,
            color: deliveryColor,
          }}
        >
          {deliveryLabel}
          {event.deliveryAttempts > 0 ? ` · ${event.deliveryAttempts}회` : ""}
        </span>
      </div>
      <div style={{ fontSize: 12, color: "var(--fg-3)" }}>
        {event.outcome} · {new Date(event.createdAt).toLocaleString("ko-KR")}
        {event.deliveryReason ? ` · ${event.deliveryReason}` : ""}
      </div>
    </section>
  );
}

function groupItems(items: InvestmentReportItem[]) {
  const buckets: Record<"action" | "watch" | "risk", InvestmentReportItem[]> = {
    action: [],
    watch: [],
    risk: [],
  };
  for (const item of items) {
    if (item.itemKind === "action" || item.itemKind === "watch" || item.itemKind === "risk") {
      buckets[item.itemKind].push(item);
    }
  }
  return buckets;
}

// ROB-322 — Korean labels for the five-section review surface. The backend
// also ships ``label_ko`` per section; we prefer our own copy for
// presentation stability and fall back to the payload label.
const REVIEW_SECTION_LABELS: Record<string, string> = {
  new_buy_candidate: "신규매수 후보",
  held_strategy_review: "보유종목 전략 변경 후보",
  watch_only: "watch-only",
  excluded_or_unavailable: "제외 / 확인 불가",
};

const NO_ACTION_KIND_LABELS: Record<string, string> = {
  real_no_action: "관망 — 데이터 충분, 신규 액션 없음",
  stale_gated: "보류 — 스냅샷 신선도 부족",
  data_insufficient: "보류 — 데이터 부족",
};

function NoActionSummaryCard({ summary }: { summary: NoActionSummary }) {
  const kindLabel = summary.kind
    ? (NO_ACTION_KIND_LABELS[summary.kind] ?? summary.kind)
    : "무액션";
  return (
    <section style={{ display: "grid", gap: 10 }}>
      <h2 style={{ margin: 0, fontSize: 18 }}>no-action 요약</h2>
      <div
        style={{
          display: "grid",
          gap: 6,
          padding: 12,
          border: "1px solid var(--border)",
          borderRadius: 12,
          background: "rgba(255,255,255,0.015)",
        }}
      >
        <strong style={{ fontSize: 14 }}>{kindLabel}</strong>
        {summary.reasonKo ? (
          <div style={{ color: "var(--fg-2)", fontSize: 13, lineHeight: 1.55 }}>
            {summary.reasonKo}
          </div>
        ) : null}
        {summary.blockingSources.length > 0 ? (
          <div style={{ color: "var(--fg-3)", fontSize: 12 }}>
            차단 소스: {summary.blockingSources.join(", ")}
          </div>
        ) : null}
        {summary.excludedCount > 0 ? (
          <div style={{ color: "var(--fg-3)", fontSize: 12 }}>
            제외 / 확인 불가 {summary.excludedCount}건
          </div>
        ) : null}
      </div>
    </section>
  );
}

function ReviewSectionsView({
  review,
  items,
  decisionsByItemUuid,
}: {
  review: ReportReviewSections;
  items: InvestmentReportItem[];
  decisionsByItemUuid: Record<string, InvestmentReportItemDecision[]>;
}) {
  const projectedUuids = new Set(
    review.sections.flatMap((section) => section.items.map((it) => it.itemUuid)),
  );
  // Defensive: never hide items the projection didn't classify (legacy items
  // mixed into a new report). They stay visible under a neutral section.
  const unprojected = items.filter((it) => !projectedUuids.has(it.itemUuid));

  return (
    <>
      {review.sections.map((section) => {
        const label =
          REVIEW_SECTION_LABELS[section.key] ?? section.labelKo ?? section.key;
        return (
          <section key={section.key} style={{ display: "grid", gap: 10 }}>
            <h2 style={{ margin: 0, fontSize: 18 }}>
              {label} ({section.items.length})
            </h2>
            {section.items.length > 0 ? (
              section.items.map((item) => (
                <ItemRow
                  key={item.itemUuid}
                  item={item}
                  decisions={decisionsByItemUuid[item.itemUuid] ?? []}
                />
              ))
            ) : (
              <div style={{ color: "var(--fg-3)", fontSize: 13 }}>
                현재 해당 종목 없음
              </div>
            )}
          </section>
        );
      })}
      {unprojected.length > 0 ? (
        <section style={{ display: "grid", gap: 10 }}>
          <h2 style={{ margin: 0, fontSize: 18 }}>분류 없음 ({unprojected.length})</h2>
          {unprojected.map((item) => (
            <ItemRow
              key={item.itemUuid}
              item={item}
              decisions={decisionsByItemUuid[item.itemUuid] ?? []}
            />
          ))}
        </section>
      ) : null}
      {review.noActionSummary ? (
        <NoActionSummaryCard summary={review.noActionSummary} />
      ) : null}
    </>
  );
}

export function InvestmentReportBundleContent({
  compact = false,
}: {
  compact?: boolean;
}) {
  const { reportUuid } = useParams<{ reportUuid: string }>();
  const { status, bundle, error, reload } = useInvestmentReportBundle(reportUuid);
  const location = useLocation();

  // ROB-500 — Discord 딥링크(`#watch-event-…` / `#watch-alert-…`)로 진입하면
  // bundle 렌더 후 해당 row로 스크롤한다.
  useEffect(() => {
    if (!bundle || !location.hash) return;
    const el = document.getElementById(location.hash.slice(1));
    el?.scrollIntoView({ block: "center" });
  }, [bundle, location.hash]);

  if (status === "loading") {
    return (
      <div style={{ padding: compact ? "14px 16px 22px" : 24 }}>
        <StatusCard>리포트를 불러오는 중…</StatusCard>
      </div>
    );
  }
  if (status === "error" || !bundle) {
    return (
      <div style={{ padding: compact ? "14px 16px 22px" : 24 }}>
        <Card>
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              gap: 12,
              alignItems: "center",
            }}
          >
            <div style={{ color: "var(--danger)", fontSize: 13 }}>
              리포트를 불러오지 못했습니다.
              {error ? ` (${error})` : ""}
            </div>
            <button
              type="button"
              onClick={reload}
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
        <div style={{ marginTop: 16 }}>
          <Link to="/reports" style={{ color: "var(--fg-2)", fontSize: 13 }}>
            ← 목록으로
          </Link>
        </div>
      </div>
    );
  }

  const buckets = groupItems(bundle.items);
  // ROB-500 — `active watches`에 triggered가 섞여 operator가 혼동하던 문제:
  // 상태별로 섹션을 분리한다.
  const activeAlerts = bundle.alerts.filter((alert) => alert.status === "active");
  const settledAlerts = bundle.alerts.filter((alert) => alert.status !== "active");

  // ROB-322 — when the backend ships the five-section projection (and it has
  // content), render the report-scoped review surface instead of the flat
  // itemKind queue. Legacy reports / older backend keep the flat grouping.
  const review = bundle.reviewSections;
  const hasReview =
    !!review &&
    (review.sections.some((section) => section.items.length > 0) ||
      !!review.noActionSummary);

  return (
    <div
      style={{
        padding: compact ? "14px 16px 22px" : 24,
        display: "grid",
        gap: 16,
      }}
    >
      <div>
        <Link to="/reports" style={{ color: "var(--fg-2)", fontSize: 13 }}>
          ← 목록으로
        </Link>
      </div>
      <ReportHeader
        title={bundle.report.title}
        market={bundle.report.market}
        marketSession={bundle.report.marketSession}
        accountScope={bundle.report.accountScope}
        executionMode={bundle.report.executionMode}
        status={bundle.report.status}
        summary={bundle.report.summary}
        riskSummary={bundle.report.riskSummary}
        thesisText={bundle.report.thesisText}
        noActionNote={bundle.report.noActionNote}
        createdAt={bundle.report.createdAt}
        freshnessSummary={bundle.report.snapshotFreshnessSummary}
        reportDiagnostics={bundle.report.snapshotReportDiagnostics}
      />

      <ReportSnapshotEvidencePanel reportUuid={bundle.report.reportUuid} />

      <IntermediateAnalysisPanel reportUuid={bundle.report.reportUuid} />

      {hasReview && review ? (
        <ReviewSectionsView
          review={review}
          items={bundle.items}
          decisionsByItemUuid={bundle.decisionsByItemUuid}
        />
      ) : (
        (["action", "watch", "risk"] as const).map((kind) =>
          buckets[kind].length > 0 ? (
            <section key={kind} style={{ display: "grid", gap: 10 }}>
              <h2 style={{ margin: 0, fontSize: 18 }}>
                {ITEM_KIND_LABELS[kind]} ({buckets[kind].length})
              </h2>
              {buckets[kind].map((item) => (
                <ItemRow
                  key={item.itemUuid}
                  item={item}
                  decisions={bundle.decisionsByItemUuid[item.itemUuid] ?? []}
                />
              ))}
            </section>
          ) : null,
        )
      )}

      {bundle.actionPacket ? (
        <ActionPacketView packet={bundle.actionPacket} />
      ) : null}

      {activeAlerts.length > 0 ? (
        <section style={{ display: "grid", gap: 10 }}>
          <h2 style={{ margin: 0, fontSize: 18 }}>
            active watches ({activeAlerts.length})
          </h2>
          {activeAlerts.map((alert) => (
            <AlertRow key={alert.alertUuid} alert={alert} />
          ))}
        </section>
      ) : null}

      {settledAlerts.length > 0 ? (
        <section style={{ display: "grid", gap: 10 }}>
          <h2 style={{ margin: 0, fontSize: 18 }}>
            triggered / closed watches ({settledAlerts.length})
          </h2>
          {settledAlerts.map((alert) => (
            <AlertRow key={alert.alertUuid} alert={alert} />
          ))}
        </section>
      ) : null}

      {bundle.events.length > 0 ? (
        <section style={{ display: "grid", gap: 10 }}>
          <h2 style={{ margin: 0, fontSize: 18 }}>
            발화 이벤트 ({bundle.events.length})
          </h2>
          {bundle.events.map((event) => (
            <EventRow key={event.eventUuid} event={event} />
          ))}
        </section>
      ) : null}
    </div>
  );
}

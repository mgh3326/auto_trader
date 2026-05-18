// ROB-265 Plan 5 — `/invest/reports/:reportUuid` detail view.
//
// Surfaces the Plan 4 delivery_status on each watch event so operators
// can see whether the Hermes notification actually reached them.

import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";

import { Card } from "../../ds";
import { useInvestmentReportBundle } from "../../hooks/useInvestmentReportBundle";
import type {
  DeliveryStatus,
  InvestmentReportItem,
  InvestmentReportItemDecision,
  InvestmentWatchAlert,
  InvestmentWatchEvent,
} from "../../types/investmentReports";

const ITEM_KIND_LABELS: Record<string, string> = {
  action: "액션",
  watch: "와치",
  risk: "리스크",
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

function ItemRow({
  item,
  decisions,
}: {
  item: InvestmentReportItem;
  decisions: InvestmentReportItemDecision[];
}) {
  const kindLabel = ITEM_KIND_LABELS[item.itemKind] ?? item.itemKind;
  const statusLabel = ITEM_STATUS_LABELS[item.status] ?? item.status;
  return (
    <section
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
      <div style={{ color: "var(--fg-2)", fontSize: 13, lineHeight: 1.55 }}>
        {item.rationale}
      </div>
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

export function InvestmentReportBundleContent({
  compact = false,
}: {
  compact?: boolean;
}) {
  const { reportUuid } = useParams<{ reportUuid: string }>();
  const { status, bundle, error, reload } = useInvestmentReportBundle(reportUuid);

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
      />

      {(
        ["action", "watch", "risk"] as const
      ).map((kind) =>
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
      )}

      {bundle.alerts.length > 0 ? (
        <section style={{ display: "grid", gap: 10 }}>
          <h2 style={{ margin: 0, fontSize: 18 }}>
            활성 와치 ({bundle.alerts.length})
          </h2>
          {bundle.alerts.map((alert) => (
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

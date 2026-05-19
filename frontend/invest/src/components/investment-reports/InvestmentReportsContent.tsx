// ROB-265 Plan 5 — `/invest/reports` list view.

import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { Card } from "../../ds";
import { useInvestmentReports } from "../../hooks/useInvestmentReports";
import type { InvestmentReport, Market } from "../../types/investmentReports";

const MARKET_LABELS: Record<Market, string> = {
  crypto: "코인",
  kr: "국내주식",
  us: "미국주식",
};

const SESSION_LABELS: Record<string, string> = {
  regular: "정규장",
  nxt: "NXT",
  pre: "장전",
  post: "장후",
  "24x7": "상시",
};

const STATUS_LABELS: Record<string, string> = {
  draft: "초안",
  published: "발행",
  decided: "결정 완료",
  expired: "만료",
  superseded: "갱신됨",
};

function SafetyHero() {
  return (
    <Card>
      <div style={{ display: "grid", gap: 9 }}>
        <div style={{ color: "var(--fg-3)", fontSize: 12, fontWeight: 900 }}>
          ROB-265 투자 리포트 워크플로우
        </div>
        <h1 style={{ margin: 0, fontSize: 28, letterSpacing: "-0.05em" }}>
          투자 리포트
        </h1>
        <p
          style={{
            margin: 0,
            color: "var(--fg-2)",
            fontSize: 14,
            lineHeight: 1.65,
          }}
        >
          리포트별 액션 · 와치 · 리스크 항목과 결정 이력을 묶어 보는 검토
          화면입니다. 와치는 재검토 트리거이며 자동 매수/매도를 발생시키지
          않습니다.
        </p>
        <div style={{ color: "var(--warn)", fontSize: 13, fontWeight: 800 }}>
          모든 결정은 advisory_only · NXT/실계좌는 자문 전용 모드만 허용
        </div>
      </div>
    </Card>
  );
}

function StatusCard({ children }: { children: ReactNode }) {
  return (
    <Card>
      <div role="status" style={{ color: "var(--fg-3)", fontSize: 13 }}>
        {children}
      </div>
    </Card>
  );
}

function reportTitle(report: InvestmentReport): string {
  const date = new Date(report.publishedAt ?? report.createdAt);
  const time = Number.isFinite(date.getTime())
    ? date.toLocaleString("ko-KR", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })
    : "시각 확인 불가";
  const market = MARKET_LABELS[report.market] ?? report.market;
  const session = report.marketSession
    ? ` · ${SESSION_LABELS[report.marketSession] ?? report.marketSession}`
    : "";
  return `${time} ${market}${session}`;
}

function ReportRow({ report }: { report: InvestmentReport }) {
  const statusLabel = STATUS_LABELS[report.status] ?? report.status;
  return (
    <Link
      to={`/reports/${report.reportUuid}`}
      style={{ color: "inherit", textDecoration: "none" }}
    >
      <section
        style={{
          display: "grid",
          gap: 8,
          padding: 14,
          border: "1px solid var(--border)",
          borderRadius: 14,
          background: "rgba(255,255,255,0.015)",
          minWidth: 0,
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "baseline",
            gap: 10,
            flexWrap: "wrap",
          }}
        >
          <h2 style={{ margin: 0, fontSize: 17, letterSpacing: "-0.03em" }}>
            {report.title || reportTitle(report)}
          </h2>
          <span
            style={{
              color: "var(--fg-3)",
              fontSize: 12,
              fontWeight: 800,
            }}
          >
            {statusLabel}
          </span>
        </div>
        <div style={{ color: "var(--fg-2)", fontSize: 13, lineHeight: 1.5 }}>
          {report.summary}
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
          <span>{reportTitle(report)}</span>
          <span>·</span>
          <span>{report.reportType}</span>
          {report.accountScope ? (
            <>
              <span>·</span>
              <span>{report.accountScope}</span>
            </>
          ) : null}
          <span>·</span>
          <span>{report.executionMode}</span>
        </div>
      </section>
    </Link>
  );
}

export function InvestmentReportsContent({
  compact = false,
}: {
  compact?: boolean;
}) {
  const { status, reports, error, reload } = useInvestmentReports({
    limit: 20,
  });

  return (
    <div
      style={{
        padding: compact ? "14px 16px 22px" : 24,
        display: "grid",
        gap: 16,
      }}
    >
      <SafetyHero />

      {status === "loading" && (
        <StatusCard>리포트 목록을 불러오는 중…</StatusCard>
      )}

      {status === "error" && (
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
              리포트 목록을 일시적으로 불러오지 못했습니다.
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
      )}

      {status === "ready" && (
        <section style={{ display: "grid", gap: 12 }}>
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "baseline",
              gap: 10,
            }}
          >
            <h2 style={{ margin: 0, fontSize: 18 }}>최근 리포트</h2>
            <span style={{ color: "var(--fg-3)", fontSize: 12 }}>
              {reports.length}건
            </span>
          </div>
          {reports.length === 0 ? (
            <StatusCard>표시할 리포트가 없습니다.</StatusCard>
          ) : (
            reports.map((report) => (
              <ReportRow key={report.reportUuid} report={report} />
            ))
          )}
        </section>
      )}
    </div>
  );
}

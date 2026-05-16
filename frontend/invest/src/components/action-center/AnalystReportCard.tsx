import { Card } from "../../ds";
import type { AnalysisReport } from "../../types/actionCenter";
import { DataVerificationPanel } from "./DataVerificationPanel";
import { StatusBadge } from "./StatusBadge";

const MARKET_LABELS: Record<string, string> = {
  crypto: "코인",
  kr: "국내주식",
  us: "미국주식",
};

const REPORT_TYPE_LABELS: Record<string, string> = {
  action_report: "액션 리포트",
  daily: "일일 리포트",
};

function dateText(value?: string | null, fallback = "시각 없음"): string {
  if (!value) return fallback;
  return new Date(value).toLocaleString("ko-KR", { dateStyle: "short", timeStyle: "short" });
}

function normalizeText(value?: string | null): string {
  if (!value) return "";
  return value
    .replace(/Crypto action report refresh: Upbit cash ([\d,]+) KRW, crypto evaluation ([\d,]+) KRW, P\/L (-?[\d.]+)%, pending orders (\d+)\. Primary candidates are ONDO and SAHARA stop-exit sells; BTC\/SOL are watch-only partial trim candidates; POLYX chase buy is rejected\. Candidate fields are stored in first-class quantity\/limit\/notional\/verification fields for action-center display\./gi, "코인 액션 리포트 갱신: Upbit 현금 $1원, 코인 평가액 $2원, 손익률 $3%, 대기 주문 $4건. 주요 후보는 ONDO·SAHARA 손절 매도, BTC·SOL은 관찰용 부분 축소, POLYX 추격 매수는 거절입니다.")
    .replace(/Crypto market is open 24\/7\. US risk assets were weak in the Naver cross-check, and crypto regulatory\/security headlines remain elevated\. The report is a read-only decision artifact; no order was submitted\./gi, "코인 시장은 24시간 열려 있습니다. Naver 교차 확인 기준 미국 위험자산은 약했고, 코인 규제·보안 관련 뉴스 위험은 높은 편입니다. 이 리포트는 읽기 전용 판단 기록이며 주문은 제출하지 않았습니다.")
    .replace(/Actual order execution not performed\./gi, "실제 주문 실행 없음")
    .replace(/Before any sell, re-check Upbit sellable quantity, staking lock status, current orderbook, and active pending orders\./gi, "매도 전 Upbit 매도 가능 수량, 스테이킹 잠금 상태, 현재 호가, 대기 주문을 재확인")
    .replace(/Use limit orders only\./gi, "지정가 주문만 사용")
    .replace(/Rejected candidates must remain non-executable\./gi, "거절 후보는 실행 불가 상태 유지");
}

function reportTypeText(value: string): string {
  return REPORT_TYPE_LABELS[value] ?? value;
}

function marketText(value: string): string {
  return MARKET_LABELS[value] ?? value;
}

export function AnalystReportCard({ report }: { report: AnalysisReport }) {
  return (
    <Card>
      <div style={{ display: "grid", gap: 14 }}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start" }}>
          <div style={{ display: "grid", gap: 5, minWidth: 0 }}>
            <div style={{ color: "var(--fg-3)", fontSize: 12, fontWeight: 800 }}>
              {reportTypeText(report.reportType)} · {marketText(report.market)} · {report.accountScope ?? "전체 계좌"}
            </div>
            <h2
              style={{
                margin: 0,
                fontSize: 18,
                letterSpacing: "-0.03em",
                lineHeight: 1.35,
                overflowWrap: "anywhere",
              }}
            >
              {normalizeText(report.summary)}
            </h2>
            <div style={{ color: "var(--fg-3)", fontSize: 12 }}>
              {report.createdByProfile} · 생성 {dateText(report.createdAt)} · 유효 {dateText(report.validUntil, "만료 시각 없음")}
            </div>
          </div>
          <StatusBadge status={report.status} />
        </div>
        {report.riskSummary && (
          <p style={{ margin: 0, color: "var(--fg-2)", fontSize: 13, lineHeight: 1.6, overflowWrap: "anywhere" }}>
            {normalizeText(report.riskSummary)}
          </p>
        )}
        <DataVerificationPanel report={report} />
        {report.safetyNotes && report.safetyNotes.length > 0 && (
          <div style={{ color: "var(--warn)", fontSize: 12, lineHeight: 1.6 }}>
            {report.safetyNotes.map((note) => <div key={note}>• {normalizeText(note)}</div>)}
          </div>
        )}
      </div>
    </Card>
  );
}

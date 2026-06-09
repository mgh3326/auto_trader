import "./screener.css";
import { InvestorFlowChip } from "./InvestorFlowChip";
import { ScreenerEmptyState } from "./ScreenerEmptyState";
import type { ScreenerResultRow, ScreenerFreshness } from "../../types/screener";

interface Props {
  rows: ScreenerResultRow[];
  metricLabel: string;
  freshness?: ScreenerFreshness | null;
}

const directionClass: Record<ScreenerResultRow["changeDirection"], string> = {
  up: "screener-change-up",
  down: "screener-change-down",
  flat: "screener-change-flat",
};

export function ScreenerResultsTable({ rows, metricLabel, freshness }: Props) {
  if (rows.length === 0) {
    return (
      <ScreenerEmptyState
        reason={freshness?.primary?.degradationReason ?? null}
        coverageLabel={freshness?.primary?.coverageLabel ?? null}
      />
    );
  }
  return (
    <table className="screener-table" data-testid="screener-results-table">
      <thead>
        <tr>
          <th aria-label="관심" />
          <th>순위</th>
          <th>종목</th>
          <th>현재가</th>
          <th>등락률</th>
          <th>카테고리</th>
          <th>시가총액</th>
          <th>거래량</th>
          <th>애널리스트 분석</th>
          <th>{metricLabel}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={`${r.market}-${r.symbol}`} data-testid="screener-row">
            <td>
              <span
                className={r.isWatched ? "screener-heart-on" : "screener-heart-off"}
                aria-label={r.isWatched ? "관심 종목" : "관심 종목 아님"}
                role="img"
              >
                ♥
              </span>
            </td>
            <td>{r.rank}</td>
            <td className="screener-name-cell">
              <span className="screener-symbol-badge">{r.symbol}</span>
              <span className="screener-symbol-name">{r.name}</span>
              {r.market === "crypto" && (
                <div className="screener-context-stack">
                  {(r.sourceContext ?? []).length > 0 && (
                    <div className="screener-context-row" aria-label="데이터 출처">
                      {(r.sourceContext ?? []).map((source) => (
                        <span
                          key={`${r.symbol}-${source.source}-${source.state}`}
                          className={`screener-context-chip screener-context-chip--${source.state}`}
                        >
                          {source.label}
                        </span>
                      ))}
                    </div>
                  )}
                  {(r.riskContext ?? []).length > 0 && (
                    <div className="screener-context-row" aria-label="리스크 맥락">
                      {(r.riskContext ?? []).map((risk) => (
                        <span
                          key={`${r.symbol}-${risk.kind}`}
                          className={`screener-risk-chip screener-risk-chip--${risk.severity}`}
                        >
                          {risk.label}
                        </span>
                      ))}
                    </div>
                  )}
                  {r.candidateContext && r.candidateContext.reasons.length > 0 && (
                    <div className="screener-candidate-context">
                      {r.candidateContext.reasons.join(" · ")}
                    </div>
                  )}
                </div>
              )}
            </td>
            <td>{r.priceLabel}</td>
            <td className={directionClass[r.changeDirection]}>
              {r.changePctLabel}
              <span className="screener-change-amount">{r.changeAmountLabel}</span>
            </td>
            <td>{r.category}</td>
            <td className="screener-cell screener-cell--market-cap">
              {r.marketCapLabel}
              {r.marketCapSource === "fallback" ? (
                <span className="screener-cell__cap-badge" title="직전 영업일 밸류에이션 스냅샷 기준">참고</span>
              ) : null}
            </td>
            <td>{r.volumeLabel}</td>
            <td>{r.analystLabel}</td>
            <td>
              {r.metricValueLabel}
              {r.investorFlowChip ? <InvestorFlowChip chip={r.investorFlowChip} /> : null}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

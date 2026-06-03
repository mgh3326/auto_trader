import type { ScreenerFreshness } from "../../types/screener";

const DATA_STATE_LABELS: Partial<Record<ScreenerFreshness["dataState"], string>> = {
  partial: "일부 데이터",
  stale: "업데이트 필요",
  missing: "데이터 준비중",
  fallback: "대체 데이터",
};

export function ScreenerFreshnessLine({
  freshness,
}: {
  freshness: ScreenerFreshness;
}) {
  const hasNewSchema =
    freshness.primary != null || freshness.servedRelativeLabel != null;

  if (!hasNewSchema) {
    // ROB-277 §D1 additive policy: legacy single-line render preserved for
    // consumers that haven't been upgraded yet.
    const text =
      freshness.source === "previous_session"
        ? `${freshness.relativeLabel} · ${freshness.asOfLabel.replace("기준", "종가")}`
        : `${freshness.asOfLabel} · ${freshness.relativeLabel}`;
    const dataState = freshness.dataState ?? "fresh";
    const stateLabel = DATA_STATE_LABELS[dataState];
    return (
      <div
        className="screener-freshness"
        data-testid="screener-freshness"
        aria-live="polite"
      >
        <span>{text}</span>
        {stateLabel ? (
          <span
            className={`screener-freshness-state screener-freshness-state--${dataState}`}
          >
            {stateLabel}
          </span>
        ) : null}
      </div>
    );
  }

  const primary = freshness.primary;
  const overall = freshness.overallState ?? freshness.dataState;
  const stateLabel = DATA_STATE_LABELS[overall];

  // Data line per the §D3 copy table.
  let dataLineText: string;
  if (freshness.source === "previous_session") {
    dataLineText = `전 거래일 기준 · ${primary?.asOfLabel ?? freshness.asOfLabel}`;
  } else if (overall === "missing") {
    dataLineText = "데이터 없음";
  } else {
    // ROB-277 §D3 follow-up: surface the worst dependency's lag info inline so
    // a stale chip's date doesn't appear next to a "fresh"-looking data line.
    const worstDep = (freshness.dependencies ?? []).find(
      (d) => d.dataState === "stale" || d.dataState === "partial",
    );
    const lagSuffix = worstDep
      ? ` · ${worstDep.lagLabel ?? (worstDep.dataState === "stale" ? "업데이트 필요" : "일부 지연")}`
      : "";
    dataLineText = `데이터 기준 ${primary?.asOfLabel ?? freshness.asOfLabel}${lagSuffix}`;
  }

  const servedLabel = freshness.servedRelativeLabel ?? "방금";

  return (
    <div
      className="screener-freshness"
      data-testid="screener-freshness"
      aria-live="polite"
    >
      <span
        className="screener-freshness-data"
        data-testid="screener-freshness-data"
      >
        {dataLineText}
        {freshness.primary?.degradationReason === "coverage_below_floor" &&
        freshness.primary?.coverageLabel ? (
          <span className="screener-freshness__coverage">
            스냅샷 적재 {freshness.primary.coverageLabel}
          </span>
        ) : null}
      </span>
      <span
        className="screener-freshness-served"
        data-testid="screener-freshness-served"
      >
        화면 갱신 {servedLabel}
      </span>
      {stateLabel ? (
        <span
          className={`screener-freshness-state screener-freshness-state--${overall}`}
        >
          {stateLabel}
        </span>
      ) : null}
    </div>
  );
}

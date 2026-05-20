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
    dataLineText = `데이터 기준 ${primary?.asOfLabel ?? freshness.asOfLabel}`;
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

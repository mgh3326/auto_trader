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

import type { ScreenerFreshness } from "../../types/screener";

export function ScreenerFreshnessLine({
  freshness,
}: {
  freshness: ScreenerFreshness;
}) {
  const text =
    freshness.source === "previous_session"
      ? `${freshness.relativeLabel} · ${freshness.asOfLabel.replace("기준", "종가")}`
      : `${freshness.asOfLabel} · ${freshness.relativeLabel}`;
  return (
    <div
      className="screener-freshness"
      data-testid="screener-freshness"
      aria-live="polite"
    >
      {text}
    </div>
  );
}

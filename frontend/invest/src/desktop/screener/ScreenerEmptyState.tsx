import type { ScreenerDegradationReason } from "../../types/screener";

interface ScreenerEmptyStateProps {
  reason: ScreenerDegradationReason | null | undefined;
  coverageLabel: string | null | undefined;
}

const COPY: Record<
  ScreenerDegradationReason,
  { title: string; tone: "neutral" | "degraded" }
> = {
  healthy_no_matches: { title: "조건에 맞는 종목이 없습니다.", tone: "neutral" },
  coverage_below_floor: {
    title: "오늘 스냅샷 커버리지가 얇아 일부만 표시됩니다.",
    tone: "degraded",
  },
  older_fallback: {
    title: "최신 스냅샷이 얇아 직전 영업일 스냅샷 기준으로 표시합니다.",
    tone: "degraded",
  },
  snapshot_missing: { title: "스크리너 스냅샷이 준비 중입니다.", tone: "degraded" },
  live: { title: "실시간 결과입니다 (스냅샷 아님).", tone: "neutral" },
};

export function ScreenerEmptyState({ reason, coverageLabel }: ScreenerEmptyStateProps) {
  const entry = reason ? COPY[reason] : null;
  if (!entry) {
    return <div className="screener-empty">표시할 종목이 없습니다.</div>;
  }
  return (
    <div className={`screener-empty screener-empty--${entry.tone}`} role="status">
      <p className="screener-empty__title">{entry.title}</p>
      {reason === "coverage_below_floor" && coverageLabel ? (
        <p className="screener-empty__coverage">스냅샷 적재: {coverageLabel}</p>
      ) : null}
    </div>
  );
}

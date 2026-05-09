import type { WeeklySummaryResponse } from "../../types/calendar";
import { Icon } from "../../ds";
import { SparkleIcon } from "./SparkleIcon";

export function AIWeeklyCard({
  summary,
  loading,
  onOpen,
  compact = false,
}: {
  summary?: WeeklySummaryResponse;
  loading?: boolean;
  onOpen: () => void;
  compact?: boolean;
}) {
  const headline = summary?.sections[0]?.title ?? "이번주 AI 요약";
  const subline = summary?.sections[0]?.body?.slice(0, 80) ?? "이번주 주요 일정 요약을 확인하세요.";

  return (
    <div
      style={{
        background: compact ? "var(--surface-2)" : "var(--ai-card-bg)",
        border: compact ? "none" : "1px solid var(--border)",
        borderRadius: 14,
        padding: 16,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          fontSize: 12,
          fontWeight: 700,
          color: "var(--accent-press)",
          marginBottom: 8,
        }}
      >
        <SparkleIcon /> 이번주 AI 요약
      </div>
      <div
        style={{
          fontSize: 14,
          fontWeight: 700,
          color: "var(--fg)",
          lineHeight: 1.45,
          letterSpacing: "-0.01em",
        }}
      >
        {loading ? "요약을 불러오는 중…" : headline}
      </div>
      {!loading && (
        <div style={{ fontSize: 12, color: "var(--fg-2)", marginTop: 6, lineHeight: 1.5 }}>
          {subline}
        </div>
      )}
      <button
        type="button"
        data-testid="open-weekly-summary"
        onClick={onOpen}
        style={{
          marginTop: 12,
          padding: 0,
          border: "none",
          background: "transparent",
          cursor: "pointer",
          color: "var(--accent-press)",
          fontWeight: 600,
          fontSize: 12,
          fontFamily: "inherit",
          display: "inline-flex",
          alignItems: "center",
          gap: 2,
        }}
      >
        자세히 보기 <Icon name="chev" size={12} />
      </button>
    </div>
  );
}

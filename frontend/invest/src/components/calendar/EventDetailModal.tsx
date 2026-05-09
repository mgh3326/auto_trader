import { useEffect } from "react";
import type { WeeklySummaryResponse } from "../../types/calendar";
import { SparkleIcon } from "./SparkleIcon";

export function EventDetailModal({
  summary,
  loading,
  error,
  onClose,
}: {
  summary?: WeeklySummaryResponse;
  loading?: boolean;
  error?: string;
  onClose: () => void;
}) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="이번주 AI 요약"
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        background: "var(--overlay)",
        zIndex: 200,
        display: "grid",
        placeItems: "center",
        padding: 24,
      }}
    >
      <div
        data-testid="weekly-summary"
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--surface)",
          borderRadius: 20,
          width: "min(820px, 100%)",
          maxHeight: "calc(100vh - 48px)",
          overflowY: "auto",
          boxShadow: "var(--shadow-3)",
        }}
      >
        <header
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            padding: "20px 28px 12px",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              fontSize: 13,
              fontWeight: 700,
              color: "var(--accent-press)",
            }}
          >
            <SparkleIcon size={16} />
            이번주 AI 요약
          </div>
          <button
            type="button"
            aria-label="닫기"
            onClick={onClose}
            style={{
              width: 32,
              height: 32,
              border: "none",
              background: "var(--surface-2)",
              borderRadius: 8,
              cursor: "pointer",
              color: "var(--fg-1)",
              fontSize: 16,
              fontFamily: "inherit",
            }}
          >
            ✕
          </button>
        </header>

        <div style={{ padding: "0 28px 24px", display: "flex", flexDirection: "column", gap: 18 }}>
          {loading && <div style={{ color: "var(--fg-3)" }}>요약을 불러오는 중…</div>}
          {error && <div style={{ color: "var(--danger)" }}>요약을 불러오지 못했습니다. ({error})</div>}
          {summary && summary.partial && (
            <div style={{ fontSize: 12, color: "var(--fg-3)" }}>
              일부 일자가 비어있습니다: {summary.missingDates.join(", ")}
            </div>
          )}
          {summary?.sections.map((section, i) => (
            <article key={i}>
              <h3
                style={{
                  margin: 0,
                  fontSize: 15,
                  fontWeight: 800,
                  color: "var(--fg)",
                  letterSpacing: "-0.01em",
                }}
              >
                {section.title}
              </h3>
              <div style={{ fontSize: 11, color: "var(--fg-3)", marginTop: 4 }}>
                {section.date} · {section.reportType}
                {section.market ? ` · ${section.market.toUpperCase()}` : ""}
              </div>
              <p
                style={{
                  margin: "8px 0 0",
                  fontSize: 13,
                  color: "var(--fg-2)",
                  lineHeight: 1.65,
                  whiteSpace: "pre-wrap",
                }}
              >
                {section.body}
              </p>
            </article>
          ))}
          {summary && summary.sections.length === 0 && !loading && !error && (
            <div style={{ color: "var(--fg-3)" }}>이번 주 요약이 아직 준비되지 않았습니다.</div>
          )}
        </div>
      </div>
    </div>
  );
}

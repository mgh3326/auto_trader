// ROB-279 Phase 5 — Single stage artifact card.
//
// Renders one StageArtifact row: stage label + verdict pill + confidence
// (right-aligned) → summary → key_points list → missing_data chips (red
// text) → footer with cited snapshot count and model name.

import type { JSX } from "react";

import type { StageArtifact, StageVerdict } from "../../types/investmentReports";
import { STAGE_TYPE_LABELS, VERDICT_LABELS } from "./stageLabels";

const VERDICT_COLORS: Record<StageVerdict, { background: string; color: string }> = {
  bull: { background: "rgba(52,199,89,0.15)", color: "var(--success, #34c759)" },
  bear: { background: "rgba(255,59,48,0.15)", color: "var(--danger, #ff3b30)" },
  neutral: { background: "rgba(142,142,147,0.15)", color: "var(--fg-3, #8e8e93)" },
  unavailable: { background: "rgba(209,209,214,0.15)", color: "var(--fg-3, #aeaeb2)" },
};

interface StageArtifactCardProps {
  artifact: StageArtifact;
}

export function StageArtifactCard({ artifact }: StageArtifactCardProps): JSX.Element {
  const stageLabel = STAGE_TYPE_LABELS[artifact.stageType] ?? artifact.stageType;
  const verdictLabel = VERDICT_LABELS[artifact.verdict] ?? artifact.verdict;
  const verdictStyle = VERDICT_COLORS[artifact.verdict] ?? VERDICT_COLORS.unavailable;

  const citationCount = artifact.citedSnapshotUuids.length;

  return (
    <article
      data-testid={`stage-card-${artifact.stageType}`}
      style={{
        display: "grid",
        gap: 10,
        padding: 14,
        border: "1px solid var(--border)",
        borderRadius: 12,
        background: "rgba(255,255,255,0.015)",
      }}
    >
      {/* Header row: stage label + verdict pill + confidence */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          gap: 10,
          flexWrap: "wrap",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontWeight: 700, fontSize: 14 }}>{stageLabel}</span>
          <span
            style={{
              fontSize: 11,
              fontWeight: 700,
              padding: "2px 8px",
              borderRadius: 100,
              background: verdictStyle.background,
              color: verdictStyle.color,
            }}
          >
            {verdictLabel}
          </span>
        </div>
        <span
          style={{
            fontSize: 13,
            fontWeight: 800,
            color: "var(--fg-2)",
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {artifact.confidence}
        </span>
      </div>

      {/* Summary */}
      {artifact.summary ? (
        <p
          style={{
            margin: 0,
            fontSize: 13,
            lineHeight: 1.6,
            color: "var(--fg-2)",
          }}
        >
          {artifact.summary}
        </p>
      ) : null}

      {/* Key points */}
      {artifact.keyPoints.length > 0 ? (
        <ul
          style={{
            margin: 0,
            paddingLeft: 18,
            display: "grid",
            gap: 4,
          }}
        >
          {artifact.keyPoints.map((point, idx) => (
            <li
              // eslint-disable-next-line react/no-array-index-key
              key={idx}
              style={{ fontSize: 13, color: "var(--fg-2)", lineHeight: 1.5 }}
            >
              {typeof point === "string" ? point : JSON.stringify(point)}
            </li>
          ))}
        </ul>
      ) : null}

      {/* Missing data chips */}
      {artifact.missingData.length > 0 ? (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {artifact.missingData.map((item, idx) => (
            <span
              // eslint-disable-next-line react/no-array-index-key
              key={idx}
              style={{
                fontSize: 11,
                color: "var(--danger, #ff3b30)",
                background: "rgba(255,59,48,0.08)",
                padding: "2px 8px",
                borderRadius: 8,
              }}
            >
              누락 데이터: {typeof item === "string" ? item : JSON.stringify(item)}
            </span>
          ))}
        </div>
      ) : null}

      {/* Footer */}
      <div
        style={{
          fontSize: 11,
          color: "var(--fg-3)",
          display: "flex",
          gap: 6,
          flexWrap: "wrap",
        }}
      >
        <span>근거 스냅샷 {citationCount}개</span>
        {artifact.modelName ? (
          <>
            <span>·</span>
            <span>{artifact.modelName}</span>
          </>
        ) : null}
      </div>
    </article>
  );
}

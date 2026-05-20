// ROB-279 Phase 5 — Intermediate analysis panel.
//
// Mounted on /invest/reports/:reportUuid below ReportSnapshotEvidencePanel.
// Lazy-loads stage artifacts via useReportStageArtifacts; renders each
// artifact as a StageArtifactCard.

import type { JSX } from "react";

import { Card } from "../../ds";
import { useReportStageArtifacts } from "../../hooks/useReportStageArtifacts";
import { StageArtifactCard } from "./StageArtifactCard";

export interface IntermediateAnalysisPanelProps {
  reportUuid: string;
}

export function IntermediateAnalysisPanel({
  reportUuid,
}: IntermediateAnalysisPanelProps): JSX.Element | null {
  const { status, artifacts, error, reload } =
    useReportStageArtifacts(reportUuid);

  if (status === "loading") {
    return (
      <Card>
        <div
          data-testid="intermediate-analysis-panel-loading"
          style={{ color: "var(--fg-3)", fontSize: 13 }}
        >
          중간 분석 로드 중…
        </div>
      </Card>
    );
  }

  if (status === "error") {
    return (
      <Card>
        <div
          data-testid="intermediate-analysis-panel-error"
          style={{
            display: "flex",
            gap: 10,
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <span style={{ color: "var(--danger)", fontSize: 13 }}>
            {error ?? "중간 분석을 불러오지 못했습니다."}
          </span>
          <button
            type="button"
            onClick={reload}
            style={{
              padding: "4px 10px",
              borderRadius: 8,
              border: "1px solid var(--border)",
              background: "transparent",
              color: "var(--fg-2)",
              cursor: "pointer",
              fontFamily: "inherit",
            }}
          >
            다시 시도
          </button>
        </div>
      </Card>
    );
  }

  if (artifacts.length === 0) {
    return (
      <Card>
        <div
          data-testid="intermediate-analysis-panel-empty"
          style={{ color: "var(--fg-3)", fontSize: 13 }}
        >
          중간 분석 결과가 없습니다 (legacy 또는 auto_compose=false 리포트).
        </div>
      </Card>
    );
  }

  return (
    <Card>
      <div
        data-testid="intermediate-analysis-panel"
        style={{ display: "grid", gap: 12 }}
      >
        <h2 style={{ margin: 0, fontSize: 18 }}>중간 분석</h2>
        {artifacts.map((artifact) => (
          <StageArtifactCard key={artifact.artifactUuid} artifact={artifact} />
        ))}
      </div>
    </Card>
  );
}

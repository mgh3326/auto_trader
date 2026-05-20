// ROB-279 Phase 5 — lazy-fetch hook for stage artifacts.
//
// Fires once on mount per (reportUuid). AbortController is cleaned up on
// unmount or when reportUuid changes. A ``reload`` helper increments an
// internal tick to re-trigger the effect.

import { useEffect, useState } from "react";

import { fetchReportStageArtifacts } from "../api/investmentStages";
import type {
  InvestmentReportRequestState,
  StageArtifact,
} from "../types/investmentReports";

interface UseReportStageArtifactsResult {
  status: InvestmentReportRequestState;
  artifacts: StageArtifact[];
  stageRunUuid: string | null;
  error: string | null;
  reload: () => void;
}

export function useReportStageArtifacts(
  reportUuid: string | undefined,
): UseReportStageArtifactsResult {
  const [status, setStatus] =
    useState<InvestmentReportRequestState>("loading");
  const [artifacts, setArtifacts] = useState<StageArtifact[]>([]);
  const [stageRunUuid, setStageRunUuid] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!reportUuid) {
      // Keep status as "loading" while uuid is not yet resolved (e.g. params
      // not ready). Do NOT set error — the parent component hasn't failed.
      return;
    }
    const controller = new AbortController();
    setStatus("loading");
    setError(null);

    fetchReportStageArtifacts(reportUuid, controller.signal)
      .then((response) => {
        setArtifacts(response.artifacts);
        setStageRunUuid(response.stageRunUuid);
        setStatus("ready");
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : String(err));
        setStatus("error");
      });

    return () => controller.abort();
  }, [reportUuid, tick]);

  return {
    status,
    artifacts,
    stageRunUuid,
    error,
    reload: () => setTick((value) => value + 1),
  };
}

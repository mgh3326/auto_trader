import { useEffect, useState } from "react";
import { fetchActionCenterCandidates, fetchActionCenterReports } from "../api/actionCenter";
import type { AnalysisCandidateQueueResponse, AnalysisReportListResponse } from "../types/actionCenter";

type State =
  | { status: "loading" }
  | { status: "ready"; reports: AnalysisReportListResponse; candidates: AnalysisCandidateQueueResponse }
  | { status: "error"; message: string };

export function useActionCenter() {
  const [state, setState] = useState<State>({ status: "loading" });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    Promise.all([fetchActionCenterReports(controller.signal), fetchActionCenterCandidates(controller.signal)])
      .then(([reports, candidates]) => setState({ status: "ready", reports, candidates }))
      .catch((err) => {
        if (controller.signal.aborted) return;
        setState({ status: "error", message: String(err?.message ?? err) });
      });
    return () => controller.abort();
  }, [nonce]);

  return { state, reload: () => setNonce((n) => n + 1) };
}

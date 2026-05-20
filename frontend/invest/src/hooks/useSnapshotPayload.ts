// ROB-275 — Lazy fetcher for a single snapshot's payload.
//
// Does NOT issue a request on mount: callers pass ``snapshotUuid``
// only when a row is clicked. Re-fetches whenever ``snapshotUuid``
// changes; aborts on unmount or change.

import { useEffect, useState } from "react";

import { fetchReportSnapshotDetail } from "../api/investmentReports";
import type {
  InvestmentReportRequestState,
  ReportSnapshotDetail,
} from "../types/investmentReports";

interface UseSnapshotPayloadResult {
  status: InvestmentReportRequestState | "idle";
  detail: ReportSnapshotDetail | null;
  error: string | null;
}

export function useSnapshotPayload(
  reportUuid: string | undefined,
  snapshotUuid: string | null,
): UseSnapshotPayloadResult {
  const [status, setStatus] = useState<
    InvestmentReportRequestState | "idle"
  >("idle");
  const [detail, setDetail] = useState<ReportSnapshotDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!reportUuid || !snapshotUuid) {
      setStatus("idle");
      setDetail(null);
      setError(null);
      return;
    }
    const controller = new AbortController();
    setStatus("loading");
    setError(null);
    setDetail(null);

    fetchReportSnapshotDetail(reportUuid, snapshotUuid, controller.signal)
      .then((response) => {
        setDetail(response);
        setStatus("ready");
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : String(err));
        setStatus("error");
      });

    return () => controller.abort();
  }, [reportUuid, snapshotUuid]);

  return { status, detail, error };
}

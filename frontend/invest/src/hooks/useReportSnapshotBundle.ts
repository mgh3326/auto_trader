// ROB-275 — eager fetch of the snapshot evidence bundle for a report.
//
// Fires once on mount per (reportUuid). Loads only metadata + per-item
// summary; the heavy payload JSON for each snapshot is fetched lazily by
// useSnapshotPayload only when the user opens a row.

import { useEffect, useState } from "react";

import { fetchReportSnapshotBundle } from "../api/investmentReports";
import type {
  InvestmentReportRequestState,
  ReportSnapshotBundle,
} from "../types/investmentReports";

interface UseReportSnapshotBundleResult {
  status: InvestmentReportRequestState;
  bundle: ReportSnapshotBundle | null;
  error: string | null;
  reload: () => void;
}

export function useReportSnapshotBundle(
  reportUuid: string | undefined,
): UseReportSnapshotBundleResult {
  const [status, setStatus] =
    useState<InvestmentReportRequestState>("loading");
  const [bundle, setBundle] = useState<ReportSnapshotBundle | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!reportUuid) {
      setStatus("error");
      setError("report_uuid is required");
      return;
    }
    const controller = new AbortController();
    setStatus("loading");
    setError(null);

    fetchReportSnapshotBundle(reportUuid, controller.signal)
      .then((response) => {
        setBundle(response);
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
    bundle,
    error,
    reload: () => setTick((value) => value + 1),
  };
}

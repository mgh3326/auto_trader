// ROB-265 Plan 5 — bundle (detail) hook for /invest/reports/:reportUuid.

import { useEffect, useState } from "react";

import { fetchInvestmentReportBundle } from "../api/investmentReports";
import type {
  InvestmentReportBundle,
  InvestmentReportRequestState,
} from "../types/investmentReports";

interface UseInvestmentReportBundleResult {
  status: InvestmentReportRequestState;
  bundle: InvestmentReportBundle | null;
  error: string | null;
  reload: () => void;
}

export function useInvestmentReportBundle(
  reportUuid: string | undefined,
): UseInvestmentReportBundleResult {
  const [status, setStatus] = useState<InvestmentReportRequestState>("loading");
  const [bundle, setBundle] = useState<InvestmentReportBundle | null>(null);
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

    fetchInvestmentReportBundle(reportUuid, controller.signal)
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

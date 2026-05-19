// ROB-265 Plan 5 — list hook for /invest/reports.

import { useEffect, useState } from "react";

import { fetchInvestmentReports } from "../api/investmentReports";
import type {
  InvestmentReport,
  InvestmentReportRequestState,
  Market,
} from "../types/investmentReports";

interface UseInvestmentReportsResult {
  status: InvestmentReportRequestState;
  reports: InvestmentReport[];
  error: string | null;
  reload: () => void;
}

export function useInvestmentReports(
  params: { market?: Market; limit?: number } = {},
): UseInvestmentReportsResult {
  const [status, setStatus] = useState<InvestmentReportRequestState>("loading");
  const [reports, setReports] = useState<InvestmentReport[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setStatus("loading");
    setError(null);

    fetchInvestmentReports(params, controller.signal)
      .then((response) => {
        setReports(response.reports);
        setStatus("ready");
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : String(err));
        setStatus("error");
      });

    return () => controller.abort();
    // ``tick`` triggers reload; param values trigger refetch when callers
    // change filters.
  }, [params.market, params.limit, tick]);

  return {
    status,
    reports,
    error,
    reload: () => setTick((value) => value + 1),
  };
}

import { useEffect, useState } from "react";
import { ApiError } from "../api/client";
import { getSessionAnalytics } from "../api/decisions";
import type { SessionAnalyticsResponse } from "../api/types";

interface AnalyticsState {
  status: "idle" | "loading" | "success" | "error" | "not_found";
  data: SessionAnalyticsResponse | null;
  error: string | null;
}

export function useSessionAnalytics(sessionUuid: string): AnalyticsState {
  const [state, setState] = useState<AnalyticsState>({
    status: "idle",
    data: null,
    error: null,
  });
  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading", data: null, error: null });
    getSessionAnalytics(sessionUuid)
      .then((data) => {
        if (!controller.signal.aborted) {
          setState({ status: "success", data, error: null });
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 404) {
          setState({ status: "not_found", data: null, error: error.detail });
          return;
        }
        setState({
          status: "error",
          data: null,
          error:
            error instanceof ApiError
              ? error.detail
              : "Could not load analytics.",
        });
      });
    return () => controller.abort();
  }, [sessionUuid]);
  return state;
}

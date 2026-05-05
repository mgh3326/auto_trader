import { useEffect, useState } from "react";
import { ApiError } from "../api/client";
import { getSymbolTimeline } from "../api/researchPipeline";
import type { SymbolTimelineResponse } from "../api/types";

interface State {
  status: "idle" | "loading" | "success" | "error" | "not_found";
  data: SymbolTimelineResponse | null;
  error: string | null;
}

export function useSymbolTimeline(symbol: string, days = 30): State {
  const [state, setState] = useState<State>({
    status: "idle",
    data: null,
    error: null,
  });

  useEffect(() => {
    const controller = new AbortController();
    setState((c) => ({ ...c, status: "loading", error: null }));

    getSymbolTimeline(symbol, days)
      .then((data) => {
        if (controller.signal.aborted) return;
        setState({ status: "success", data, error: null });
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
              : "Something went wrong. Try again.",
        });
      });

    return () => controller.abort();
  }, [symbol, days]);

  return state;
}

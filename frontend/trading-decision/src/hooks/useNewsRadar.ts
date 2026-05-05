// frontend/trading-decision/src/hooks/useNewsRadar.ts
import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../api/client";
import { getNewsRadar } from "../api/newsRadar";
import type { NewsRadarFilters, NewsRadarResponse } from "../api/types";

export const DEFAULT_NEWS_RADAR_FILTERS: NewsRadarFilters = {
  market: "all",
  hours: 24,
  q: "",
  riskCategory: "",
  includeExcluded: true,
  limit: 50,
};

type State =
  | { status: "loading"; data: NewsRadarResponse | null; error: null }
  | { status: "success"; data: NewsRadarResponse; error: null }
  | { status: "error"; data: null; error: string };

export interface UseNewsRadarResult {
  status: State["status"];
  data: NewsRadarResponse | null;
  error: string | null;
  filters: NewsRadarFilters;
  setFilters: (
    next: NewsRadarFilters | ((prev: NewsRadarFilters) => NewsRadarFilters),
  ) => void;
  refetch: () => void;
}

export function useNewsRadar(
  initial: NewsRadarFilters = DEFAULT_NEWS_RADAR_FILTERS,
): UseNewsRadarResult {
  const [filters, setFiltersState] = useState<NewsRadarFilters>(initial);
  const [version, setVersion] = useState(0);
  const [state, setState] = useState<State>({
    status: "loading",
    data: null,
    error: null,
  });

  const setFilters: UseNewsRadarResult["setFilters"] = useCallback((next) => {
    setFiltersState((prev) =>
      typeof next === "function" ? (next as (p: NewsRadarFilters) => NewsRadarFilters)(prev) : next,
    );
  }, []);

  const refetch = useCallback(() => setVersion((v) => v + 1), []);

  useEffect(() => {
    let aborted = false;
    setState({ status: "loading", data: null, error: null });
    getNewsRadar(filters)
      .then((data) => {
        if (aborted) return;
        setState({ status: "success", data, error: null });
      })
      .catch((err: unknown) => {
        if (aborted) return;
        const message =
          err instanceof ApiError ? err.detail : "Failed to load news radar.";
        setState({ status: "error", data: null, error: message });
      });
    return () => {
      aborted = true;
    };
  }, [filters, version]);

  return {
    status: state.status,
    data: state.data,
    error: state.error,
    filters,
    setFilters,
    refetch,
  };
}

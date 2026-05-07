// frontend/invest/src/hooks/useNewsIssues.ts
import { useEffect, useMemo, useState } from "react";
import { fetchNewsIssues, type FetchNewsIssuesParams } from "../api/newsIssues";
import type { MarketIssuesResponse } from "../types/newsIssues";

export type NewsIssuesState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: MarketIssuesResponse };

export interface UseNewsIssuesOptions {
  enabled?: boolean;
}

export function useNewsIssues(
  params: FetchNewsIssuesParams = {},
  options: UseNewsIssuesOptions = {},
) {
  const enabled = options.enabled ?? true;
  const [state, setState] = useState<NewsIssuesState>({ status: "loading" });
  const [tick, setTick] = useState(0);
  const paramsKey = useMemo(() => JSON.stringify(params), [params]);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setState({ status: "loading" });
    fetchNewsIssues(params, controller.signal)
      .then((data) => setState({ status: "ready", data }))
      .catch((e) => {
        if (controller.signal.aborted) return;
        setState({ status: "error", message: e?.message ?? "failed" });
      });
    return () => controller.abort();
    // paramsKey keeps object params from refetching every render while still reacting to meaningful changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, paramsKey, tick]);

  return { state, reload: () => setTick((t) => t + 1) };
}

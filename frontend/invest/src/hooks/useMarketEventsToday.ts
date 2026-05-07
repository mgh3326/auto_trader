import { useEffect, useMemo, useState } from "react";
import {
  fetchMarketEventsToday,
} from "../api/marketEvents";
import type {
  FetchMarketEventsTodayParams,
  MarketEventsDayResponse,
} from "../types/marketEvents";

export type MarketEventsTodayState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: MarketEventsDayResponse };

export interface UseMarketEventsTodayOptions {
  enabled?: boolean;
}

export function useMarketEventsToday(
  params: FetchMarketEventsTodayParams = {},
  options: UseMarketEventsTodayOptions = {},
) {
  const enabled = options.enabled ?? true;
  const [state, setState] = useState<MarketEventsTodayState>({ status: "loading" });
  const [tick, setTick] = useState(0);
  const paramsKey = useMemo(() => JSON.stringify(params), [params]);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setState({ status: "loading" });
    fetchMarketEventsToday(params, controller.signal)
      .then((data) => setState({ status: "ready", data }))
      .catch((e) => {
        if (controller.signal.aborted) return;
        setState({ status: "error", message: e?.message ?? "failed" });
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, paramsKey, tick]);

  return { state, reload: () => setTick((t) => t + 1) };
}

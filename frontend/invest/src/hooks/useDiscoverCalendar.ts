import { useEffect, useMemo, useState } from "react";
import { fetchDiscoverCalendar } from "../api/marketEvents";
import type {
  DiscoverCalendarResponse,
  FetchDiscoverCalendarParams,
} from "../types/marketEvents";

export type DiscoverCalendarState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: DiscoverCalendarResponse };

export interface UseDiscoverCalendarOptions {
  enabled?: boolean;
}

export function useDiscoverCalendar(
  params: FetchDiscoverCalendarParams,
  options: UseDiscoverCalendarOptions = {},
) {
  const enabled = options.enabled ?? true;
  const [state, setState] = useState<DiscoverCalendarState>({ status: "loading" });
  const [tick, setTick] = useState(0);
  const paramsKey = useMemo(() => JSON.stringify(params), [params]);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setState({ status: "loading" });
    fetchDiscoverCalendar(params, controller.signal)
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

// frontend/invest/src/hooks/useNewsRadar.ts
import { useEffect, useState } from "react";
import { fetchNewsRadar, type FetchNewsRadarParams } from "../api/newsRadar";
import type { NewsRadarResponse } from "../types/newsRadar";

export type NewsRadarState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: NewsRadarResponse };

export function useNewsRadar(params: FetchNewsRadarParams = {}) {
  const [state, setState] = useState<NewsRadarState>({ status: "loading" });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    fetchNewsRadar(params, controller.signal)
      .then((data) => setState({ status: "ready", data }))
      .catch((e) => {
        if (controller.signal.aborted) return;
        setState({ status: "error", message: e?.message ?? "failed" });
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick]);

  return { state, reload: () => setTick((t) => t + 1) };
}

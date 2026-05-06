import { useEffect, useState } from "react";
import { fetchInvestHome } from "../api/investHome";
import type { InvestHomeResponse } from "../types/invest";

export type InvestHomeState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: InvestHomeResponse };

export function useInvestHome() {
  const [state, setState] = useState<InvestHomeState>({ status: "loading" });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    fetchInvestHome(controller.signal)
      .then((data) => setState({ status: "ready", data }))
      .catch((e) => {
        if (controller.signal.aborted) return;
        setState({ status: "error", message: e?.message ?? "failed" });
      });
    return () => controller.abort();
  }, [tick]);

  return { state, reload: () => setTick((t) => t + 1) };
}

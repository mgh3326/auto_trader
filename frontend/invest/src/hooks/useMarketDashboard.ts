import { useEffect, useState } from "react";
import { fetchMarketDashboard } from "../api/marketDashboard";
import type { MarketDashboardResponse } from "../types/marketDashboard";

type State =
  | { status: "loading" }
  | { status: "ready"; data: MarketDashboardResponse }
  | { status: "error"; message: string };

export function useMarketDashboard() {
  const [state, setState] = useState<State>({ status: "loading" });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    fetchMarketDashboard(controller.signal)
      .then((data) => setState({ status: "ready", data }))
      .catch((err) => {
        if (controller.signal.aborted) return;
        setState({ status: "error", message: String(err?.message ?? err) });
      });
    return () => controller.abort();
  }, [nonce]);

  return { state, reload: () => setNonce((n) => n + 1) };
}

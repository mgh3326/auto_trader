import { useEffect, useState } from "react";
import { fetchFxDashboard } from "../api/fxDashboard";
import type { FxDashboardResponse } from "../types/fxDashboard";

type State =
  | { status: "loading" }
  | { status: "ready"; data: FxDashboardResponse }
  | { status: "error"; message: string };

export function useFxDashboard() {
  const [state, setState] = useState<State>({ status: "loading" });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    fetchFxDashboard(controller.signal)
      .then((data) => setState({ status: "ready", data }))
      .catch((err) => {
        if (controller.signal.aborted) return;
        setState({ status: "error", message: String(err?.message ?? err) });
      });
    return () => controller.abort();
  }, [nonce]);

  return { state, reload: () => setNonce((n) => n + 1) };
}

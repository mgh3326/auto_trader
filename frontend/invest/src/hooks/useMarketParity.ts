import { useEffect, useState } from "react";
import { fetchMarketParity } from "../api/marketParity";
import type { MarketParityResponse } from "../types/marketParity";

export type MarketParityHookState =
  | { status: "loading" }
  | { status: "ready"; data: MarketParityResponse }
  | { status: "error"; message: string };

export function useMarketParity() {
  const [state, setState] = useState<MarketParityHookState>({ status: "loading" });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    fetchMarketParity(controller.signal)
      .then((data) => setState({ status: "ready", data }))
      .catch((err) => {
        if (controller.signal.aborted) return;
        setState({ status: "error", message: String(err?.message ?? err) });
      });
    return () => controller.abort();
  }, [nonce]);

  return { state, reload: () => setNonce((n) => n + 1) };
}

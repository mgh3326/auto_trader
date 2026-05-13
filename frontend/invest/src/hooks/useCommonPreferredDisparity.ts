import { useEffect, useState } from "react";
import { fetchCommonPreferredDisparity } from "../api/commonPreferredDisparity";
import type { CommonPreferredDisparityResponse } from "../types/commonPreferredDisparity";

type State =
  | { status: "loading" }
  | { status: "ready"; data: CommonPreferredDisparityResponse }
  | { status: "error"; message: string };

export function useCommonPreferredDisparity() {
  const [state, setState] = useState<State>({ status: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    fetchCommonPreferredDisparity({ symbols: "005930,005935", limit: 6, signal: controller.signal })
      .then((data) => setState({ status: "ready", data }))
      .catch((err) => {
        if (controller.signal.aborted) return;
        setState({ status: "error", message: String(err?.message ?? err) });
      });
    return () => controller.abort();
  }, []);

  return state;
}

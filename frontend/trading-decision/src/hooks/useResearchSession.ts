import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { getSessionFull } from "../api/researchPipeline";
import type { ResearchSessionFullResponse } from "../api/types";

const POLL_INTERVAL_MS = 5000;
const TERMINAL_STATUSES = new Set(["finalized", "failed", "cancelled"]);

interface State {
  status: "idle" | "loading" | "success" | "error" | "not_found";
  data: ResearchSessionFullResponse | null;
  error: string | null;
}

export function useResearchSession(sessionId: number): State & {
  refetch: () => void;
} {
  const [state, setState] = useState<State>({
    status: "idle",
    data: null,
    error: null,
  });
  const [version, setVersion] = useState(0);
  const refetch = useCallback(() => setVersion((v) => v + 1), []);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let cancelled = false;

    setState((current) => ({ ...current, status: "loading", error: null }));

    async function fetchOnce() {
      try {
        const data = await getSessionFull(sessionId);
        if (cancelled) return;
        setState({ status: "success", data, error: null });

        if (!TERMINAL_STATUSES.has(data.session.status)) {
          timerRef.current = setTimeout(fetchOnce, POLL_INTERVAL_MS);
        }
      } catch (error: unknown) {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 404) {
          setState({ status: "not_found", data: null, error: error.detail });
          return;
        }
        setState({
          status: "error",
          data: null,
          error:
            error instanceof ApiError
              ? error.detail
              : "Something went wrong. Try again.",
        });
      }
    }

    fetchOnce();

    return () => {
      cancelled = true;
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [sessionId, version]);

  return { ...state, refetch };
}

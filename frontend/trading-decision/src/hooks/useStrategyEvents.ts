import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../api/client";
import {
  createStrategyEvent,
  getStrategyEvents,
} from "../api/strategyEvents";
import type {
  StrategyEventCreateRequest,
  StrategyEventListResponse,
} from "../api/types";

interface StrategyEventsState {
  status: "idle" | "loading" | "success" | "error" | "not_found";
  data: StrategyEventListResponse | null;
  error: string | null;
}

export interface UseStrategyEventsResult extends StrategyEventsState {
  refetch: () => void;
  submit: (
    body: StrategyEventCreateRequest,
  ) => Promise<{ ok: boolean; status?: number; detail?: string }>;
}

export function useStrategyEvents(
  sessionUuid: string,
): UseStrategyEventsResult {
  const [state, setState] = useState<StrategyEventsState>({
    status: "idle",
    data: null,
    error: null,
  });
  const [version, setVersion] = useState(0);
  const refetch = useCallback(() => setVersion((v) => v + 1), []);

  useEffect(() => {
    if (!sessionUuid) return;
    const controller = new AbortController();
    setState((current) => ({ ...current, status: "loading", error: null }));
    getStrategyEvents({ sessionUuid })
      .then((data) => {
        if (!controller.signal.aborted) {
          setState({ status: "success", data, error: null });
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 404) {
          setState({
            status: "not_found",
            data: null,
            error: error.detail,
          });
          return;
        }
        setState({
          status: "error",
          data: null,
          error:
            error instanceof ApiError
              ? error.detail
              : "Could not load strategy events.",
        });
      });
    return () => controller.abort();
  }, [sessionUuid, version]);

  async function submit(body: StrategyEventCreateRequest) {
    try {
      await createStrategyEvent(body);
      refetch();
      return { ok: true };
    } catch (error) {
      if (error instanceof ApiError) {
        return { ok: false, status: error.status, detail: error.detail };
      }
      return { ok: false, detail: "Could not submit strategy event." };
    }
  }

  return { ...state, refetch, submit };
}

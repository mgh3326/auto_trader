import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../api/client";
import { getDecisions } from "../api/decisions";
import type { SessionListResponse, SessionStatus } from "../api/types";

interface InboxState {
  status: "idle" | "loading" | "success" | "error";
  data: SessionListResponse | null;
  error: string | null;
}

export function useDecisionInbox(args: {
  limit: number;
  offset: number;
  statusFilter?: SessionStatus;
}): InboxState & { refetch: () => void } {
  const [state, setState] = useState<InboxState>({
    status: "idle",
    data: null,
    error: null,
  });
  const [version, setVersion] = useState(0);

  const refetch = useCallback(() => setVersion((current) => current + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    setState((current) => ({ ...current, status: "loading", error: null }));
    getDecisions({
      limit: args.limit,
      offset: args.offset,
      status: args.statusFilter,
    })
      .then((data) => {
        if (!controller.signal.aborted) {
          setState({ status: "success", data, error: null });
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401) {
          redirectToLogin();
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
      });
    return () => controller.abort();
  }, [args.limit, args.offset, args.statusFilter, version]);

  return { ...state, refetch };
}

function redirectToLogin() {
  if (typeof window === "undefined") return;
  window.location.assign(
    `/login?next=${encodeURIComponent(window.location.pathname)}`,
  );
}

import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../api/client";
import {
  createOutcomeMark,
  getSession,
  respondToProposal,
} from "../api/decisions";
import type {
  OutcomeCreateRequest,
  ProposalRespondRequest,
  SessionDetail,
} from "../api/types";

interface SessionState {
  status: "idle" | "loading" | "success" | "error" | "not_found";
  data: SessionDetail | null;
  error: string | null;
}

export function useDecisionSession(sessionUuid: string): SessionState & {
  refetch: () => void;
  respond: (
    proposalUuid: string,
    body: ProposalRespondRequest,
  ) => Promise<{ ok: boolean; status?: number; detail?: string }>;
  recordOutcome: (
    proposalUuid: string,
    body: OutcomeCreateRequest,
  ) => Promise<{ ok: boolean; status?: number; detail?: string }>;
} {
  const [state, setState] = useState<SessionState>({
    status: "idle",
    data: null,
    error: null,
  });
  const [version, setVersion] = useState(0);
  const refetch = useCallback(() => setVersion((current) => current + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    setState((current) => ({ ...current, status: "loading", error: null }));
    getSession(sessionUuid)
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
      });
    return () => controller.abort();
  }, [sessionUuid, version]);

  async function respond(proposalUuid: string, body: ProposalRespondRequest) {
    try {
      await respondToProposal(proposalUuid, body);
      refetch();
      return { ok: true };
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        redirectToLogin();
        return { ok: false, status: 401, detail: error.detail };
      }
      if (error instanceof ApiError) {
        return { ok: false, status: error.status, detail: error.detail };
      }
      return { ok: false, detail: "Something went wrong. Try again." };
    }
  }

  async function recordOutcome(proposalUuid: string, body: OutcomeCreateRequest) {
    try {
      await createOutcomeMark(proposalUuid, body);
      refetch();
      return { ok: true };
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        redirectToLogin();
        return { ok: false, status: 401, detail: error.detail };
      }
      if (error instanceof ApiError) {
        return { ok: false, status: error.status, detail: error.detail };
      }
      return { ok: false, detail: "Something went wrong. Try again." };
    }
  }

  return { ...state, refetch, respond, recordOutcome };
}

function redirectToLogin() {
  if (typeof window === "undefined") return;
  window.location.assign(
    `/login?next=${encodeURIComponent(window.location.pathname)}`,
  );
}

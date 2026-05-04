import { useCallback, useState } from "react";
import { updateArtifacts, updateWorkflowStatus } from "../api/decisions";
import type { SessionDetail, WorkflowStatus } from "../api/types";

export function useCommitteeWorkflow(
  initialSession: SessionDetail,
  onUpdate?: (updated: SessionDetail) => void,
) {
  const [session, setSession] = useState<SessionDetail>(initialSession);
  const [isUpdating, setIsUpdating] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const transitionTo = useCallback(
    async (nextStatus: WorkflowStatus) => {
      setIsUpdating(true);
      setError(null);
      try {
        const updated = await updateWorkflowStatus(
          session.session_uuid,
          nextStatus,
        );
        setSession(updated);
        onUpdate?.(updated);
        return updated;
      } catch (err) {
        const error = err instanceof Error ? err : new Error("Failed to transition");
        setError(error);
        throw error;
      } finally {
        setIsUpdating(false);
      }
    },
    [session.session_uuid, onUpdate],
  );

  const patchArtifacts = useCallback(
    async (patch: Record<string, unknown>) => {
      setIsUpdating(true);
      setError(null);
      try {
        const updated = await updateArtifacts(session.session_uuid, patch);
        setSession(updated);
        onUpdate?.(updated);
        return updated;
      } catch (err) {
        const error = err instanceof Error ? err : new Error("Failed to update artifacts");
        setError(error);
        throw error;
      } finally {
        setIsUpdating(false);
      }
    },
    [session.session_uuid, onUpdate],
  );

  return {
    session,
    isUpdating,
    error,
    transitionTo,
    patchArtifacts,
  };
}

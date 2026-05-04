import { renderHook, act } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { useCommitteeWorkflow } from "../../hooks/useCommitteeWorkflow";
import * as api from "../../api/decisions";
import { makeSessionDetail } from "../../test/fixtures";

vi.mock("../../api/decisions");

describe("useCommitteeWorkflow", () => {
  const initialSession = makeSessionDetail({
    workflow_status: "created",
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("transitions to a new status", async () => {
    const updatedSession = { ...initialSession, workflow_status: "evidence_ready" };
    vi.mocked(api.updateWorkflowStatus).mockResolvedValue(updatedSession as any);

    const { result } = renderHook(() => useCommitteeWorkflow(initialSession as any));

    await act(async () => {
      const updated = await result.current.transitionTo("evidence_ready");
      expect(updated.workflow_status).toBe("evidence_ready");
    });

    expect(result.current.session.workflow_status).toBe("evidence_ready");
    expect(api.updateWorkflowStatus).toHaveBeenCalledWith(
      initialSession.session_uuid,
      "evidence_ready"
    );
  });

  it("patches artifacts", async () => {
    const patch = { evidence: { news: "ok" } };
    const updatedSession = { ...initialSession, artifacts: patch };
    vi.mocked(api.updateArtifacts).mockResolvedValue(updatedSession as any);

    const { result } = renderHook(() => useCommitteeWorkflow(initialSession as any));

    await act(async () => {
      await result.current.patchArtifacts(patch);
    });

    expect(result.current.session.artifacts).toEqual(patch);
    expect(api.updateArtifacts).toHaveBeenCalledWith(
      initialSession.session_uuid,
      patch
    );
  });
});

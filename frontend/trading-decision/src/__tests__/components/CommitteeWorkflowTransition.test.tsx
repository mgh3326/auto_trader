import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { CommitteeWorkflowTransition } from "../../components/CommitteeWorkflowTransition";

describe("CommitteeWorkflowTransition", () => {
  it("renders the next step button", () => {
    const onTransition = vi.fn();
    render(
      <CommitteeWorkflowTransition
        currentStatus="created"
        isUpdating={false}
        onTransition={onTransition}
      />
    );

    const button = screen.getByRole("button", { name: /Advance to evidence generating/i });
    expect(button).toBeInTheDocument();
    
    fireEvent.click(button);
    expect(onTransition).toHaveBeenCalledWith("evidence_generating");
  });

  it("disables button when updating", () => {
    render(
      <CommitteeWorkflowTransition
        currentStatus="created"
        isUpdating={true}
        onTransition={() => {}}
      />
    );

    const button = screen.getByRole("button", { name: /Updating.../i });
    expect(button).toBeDisabled();
  });

  it("renders nothing if status is terminal or null", () => {
    const { container } = render(
      <CommitteeWorkflowTransition
        currentStatus="completed"
        isUpdating={false}
        onTransition={() => {}}
      />
    );
    expect(container.firstChild).toBeNull();
  });
});

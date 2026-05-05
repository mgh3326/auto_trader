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

  it("routes risk_review_ready to auto_approved for kis_mock sessions", () => {
    const onTransition = vi.fn();
    render(
      <CommitteeWorkflowTransition
        currentStatus="risk_review_ready"
        accountMode="kis_mock"
        isUpdating={false}
        onTransition={onTransition}
      />
    );
    fireEvent.click(
      screen.getByRole("button", { name: /Advance to auto approved/i }),
    );
    expect(onTransition).toHaveBeenCalledWith("auto_approved");
  });

  it("routes risk_review_ready to auto_approved for alpaca_paper sessions", () => {
    const onTransition = vi.fn();
    render(
      <CommitteeWorkflowTransition
        currentStatus="risk_review_ready"
        accountMode="alpaca_paper"
        isUpdating={false}
        onTransition={onTransition}
      />
    );
    fireEvent.click(
      screen.getByRole("button", { name: /Advance to auto approved/i }),
    );
    expect(onTransition).toHaveBeenCalledWith("auto_approved");
  });

  it("skips auto_approved for non-simulation modes (kis_live, db_simulated)", () => {
    const onTransition = vi.fn();
    render(
      <CommitteeWorkflowTransition
        currentStatus="risk_review_ready"
        accountMode="kis_live"
        isUpdating={false}
        onTransition={onTransition}
      />
    );
    fireEvent.click(
      screen.getByRole("button", { name: /Advance to preview ready/i }),
    );
    expect(onTransition).toHaveBeenCalledWith("preview_ready");
  });

  it("advances auto_approved to preview_ready", () => {
    const onTransition = vi.fn();
    render(
      <CommitteeWorkflowTransition
        currentStatus="auto_approved"
        accountMode="alpaca_paper"
        isUpdating={false}
        onTransition={onTransition}
      />
    );
    fireEvent.click(
      screen.getByRole("button", { name: /Advance to preview ready/i }),
    );
    expect(onTransition).toHaveBeenCalledWith("preview_ready");
  });
});

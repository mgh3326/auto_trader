import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { CommitteeWorkflowTransition } from "../../components/CommitteeWorkflowTransition";

describe("CommitteeWorkflowTransition", () => {
  it("renders the next step button", () => {
    const onTransition = vi.fn();
    render(
      <CommitteeWorkflowTransition
        currentStatus="created"
        accountMode="kis_mock"
        isUpdating={false}
        onTransition={onTransition}
      />
    );

    const button = screen.getByRole("button", { name: /근거 수집 중 단계로 진행/i });
    expect(button).toBeInTheDocument();
    
    fireEvent.click(button);
    expect(onTransition).toHaveBeenCalledWith("evidence_generating");
  });

  it("disables button when updating", () => {
    render(
      <CommitteeWorkflowTransition
        currentStatus="created"
        accountMode="kis_mock"
        isUpdating={true}
        onTransition={() => {}}
      />
    );

    const button = screen.getByRole("button", { name: /저장 중…/i });
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
      screen.getByRole("button", { name: /자동 승인 단계로 진행/i }),
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
      screen.getByRole("button", { name: /자동 승인 단계로 진행/i }),
    );
    expect(onTransition).toHaveBeenCalledWith("auto_approved");
  });

  it("renders nothing for non-simulation modes (kis_live, db_simulated)", () => {
    const onTransition = vi.fn();
    const { container } = render(
      <CommitteeWorkflowTransition
        currentStatus="risk_review_ready"
        accountMode="kis_live"
        isUpdating={false}
        onTransition={onTransition}
      />,
    );
    expect(container.firstChild).toBeNull();
    expect(onTransition).not.toHaveBeenCalled();
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
      screen.getByRole("button", { name: /프리뷰 준비 단계로 진행/i }),
    );
    expect(onTransition).toHaveBeenCalledWith("preview_ready");
  });
});

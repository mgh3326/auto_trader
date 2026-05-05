import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import ProposalResponseControls from "../components/ProposalResponseControls";

describe("ProposalResponseControls", () => {
  it("renders the five response buttons", () => {
    render(
      <ProposalResponseControls
        currentResponse="pending"
        isSubmitting={false}
        onOpenAdjust={vi.fn()}
        onSimpleResponse={vi.fn()}
      />,
    );

    for (const name of ["수락", "부분 수락", "수정", "보류", "거절"]) {
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    }
  });

  it("clicking 수락 calls onSimpleResponse", async () => {
    const onSimpleResponse = vi.fn();
    render(
      <ProposalResponseControls
        currentResponse="pending"
        isSubmitting={false}
        onOpenAdjust={vi.fn()}
        onSimpleResponse={onSimpleResponse}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "수락" }));

    expect(onSimpleResponse).toHaveBeenCalledWith("accept");
  });

  it("clicking 수정 opens the adjustment editor", async () => {
    const onOpenAdjust = vi.fn();
    render(
      <ProposalResponseControls
        currentResponse="pending"
        isSubmitting={false}
        onOpenAdjust={onOpenAdjust}
        onSimpleResponse={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "수정" }));

    expect(onOpenAdjust).toHaveBeenCalledWith("modify");
  });

  it("disables buttons while submitting", () => {
    render(
      <ProposalResponseControls
        currentResponse="pending"
        isSubmitting
        onOpenAdjust={vi.fn()}
        onSimpleResponse={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "수락" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "수정" })).toBeDisabled();
  });
});

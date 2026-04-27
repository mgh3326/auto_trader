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

    for (const name of ["Accept", "Partial accept", "Modify", "Defer", "Reject"]) {
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    }
  });

  it("clicking accept calls onSimpleResponse", async () => {
    const onSimpleResponse = vi.fn();
    render(
      <ProposalResponseControls
        currentResponse="pending"
        isSubmitting={false}
        onOpenAdjust={vi.fn()}
        onSimpleResponse={onSimpleResponse}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Accept" }));

    expect(onSimpleResponse).toHaveBeenCalledWith("accept");
  });

  it("clicking modify opens the adjustment editor", async () => {
    const onOpenAdjust = vi.fn();
    render(
      <ProposalResponseControls
        currentResponse="pending"
        isSubmitting={false}
        onOpenAdjust={onOpenAdjust}
        onSimpleResponse={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Modify" }));

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

    expect(screen.getByRole("button", { name: "Accept" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Modify" })).toBeDisabled();
  });
});

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import ProposalAdjustmentEditor from "../components/ProposalAdjustmentEditor";
import { makeProposal } from "../test/fixtures";

describe("ProposalAdjustmentEditor", () => {
  it("shows numeric fields only for present original fields", () => {
    render(
      <ProposalAdjustmentEditor
        proposal={makeProposal({ original_amount: null })}
        response="modify"
        onCancel={vi.fn()}
        onSubmit={vi.fn()}
      />,
    );

    expect(screen.getByLabelText("Quantity percent")).toBeInTheDocument();
    expect(screen.getByLabelText("Price")).toBeInTheDocument();
    expect(screen.queryByLabelText("Amount")).not.toBeInTheDocument();
  });

  it("uses original values as placeholders", () => {
    render(
      <ProposalAdjustmentEditor
        proposal={makeProposal()}
        response="partial_accept"
        onCancel={vi.fn()}
        onSubmit={vi.fn()}
      />,
    );

    expect(screen.getByLabelText("Quantity percent")).toHaveAttribute(
      "placeholder",
      "20",
    );
  });

  it("rejects submit when no numeric fields are set", async () => {
    const onSubmit = vi.fn();
    render(
      <ProposalAdjustmentEditor
        proposal={makeProposal()}
        response="modify"
        onCancel={vi.fn()}
        onSubmit={onSubmit}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Save modify" }));

    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Enter at least one adjusted numeric value.",
    );
  });

  it("submits exact decimal strings", async () => {
    const onSubmit = vi.fn().mockResolvedValue({ ok: true });
    render(
      <ProposalAdjustmentEditor
        proposal={makeProposal()}
        response="modify"
        onCancel={vi.fn()}
        onSubmit={onSubmit}
      />,
    );

    await userEvent.type(screen.getByLabelText("Quantity percent"), "10");
    await userEvent.click(screen.getByRole("button", { name: "Save modify" }));

    expect(onSubmit).toHaveBeenCalledWith({
      response: "modify",
      user_quantity_pct: "10",
    });
  });

  it("keeps the editor open and shows server detail on 422", async () => {
    const onSubmit = vi.fn().mockResolvedValue({
      ok: false,
      detail: "modify/partial_accept requires at least one user_* numeric field",
    });
    render(
      <ProposalAdjustmentEditor
        proposal={makeProposal()}
        response="modify"
        onCancel={vi.fn()}
        onSubmit={onSubmit}
      />,
    );

    await userEvent.type(screen.getByLabelText("Quantity percent"), "10");
    await userEvent.click(screen.getByRole("button", { name: "Save modify" }));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "modify/partial_accept requires at least one user_* numeric field",
    );
    expect(screen.getByLabelText("Quantity percent")).toBeInTheDocument();
  });
});

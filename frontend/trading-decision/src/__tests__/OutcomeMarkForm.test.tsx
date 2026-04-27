import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import OutcomeMarkForm from "../components/OutcomeMarkForm";
import { makeCounterfactual } from "../test/fixtures";

describe("OutcomeMarkForm", () => {
  it("submits an accepted_live mark with no counterfactual_id", async () => {
    const onSubmit = vi.fn().mockResolvedValue({ ok: true });
    render(<OutcomeMarkForm counterfactuals={[]} onSubmit={onSubmit} />);

    await userEvent.selectOptions(
      screen.getByLabelText(/track/i),
      "accepted_live",
    );
    await userEvent.selectOptions(screen.getByLabelText(/horizon/i), "1h");
    await userEvent.type(screen.getByLabelText(/price at mark/i), "100");
    await userEvent.click(screen.getByRole("button", { name: /record mark/i }));

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        track_kind: "accepted_live",
        horizon: "1h",
        price_at_mark: "100",
      }),
    );
    expect(onSubmit.mock.calls[0]?.[0].counterfactual_id).toBeUndefined();
  });

  it("blocks submit when non-accepted-live track has no counterfactual_id selected", async () => {
    const onSubmit = vi.fn();
    render(<OutcomeMarkForm counterfactuals={[]} onSubmit={onSubmit} />);

    await userEvent.selectOptions(
      screen.getByLabelText(/track/i),
      "rejected_counterfactual",
    );
    await userEvent.selectOptions(screen.getByLabelText(/horizon/i), "1h");
    await userEvent.type(screen.getByLabelText(/price at mark/i), "100");
    await userEvent.click(screen.getByRole("button", { name: /record mark/i }));

    expect(onSubmit).not.toHaveBeenCalled();
    expect(
      screen.getByText(/counterfactual is required/i),
    ).toBeInTheDocument();
  });

  it("offers counterfactual options when one is provided", async () => {
    const cf = makeCounterfactual({
      id: 11,
      track_kind: "rejected_counterfactual",
    });
    render(<OutcomeMarkForm counterfactuals={[cf]} onSubmit={vi.fn()} />);
    await userEvent.selectOptions(
      screen.getByLabelText(/track/i),
      "rejected_counterfactual",
    );
    expect(screen.getByLabelText(/counterfactual/i)).toBeInTheDocument();
  });
});

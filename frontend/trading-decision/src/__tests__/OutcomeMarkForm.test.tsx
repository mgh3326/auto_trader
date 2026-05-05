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
      screen.getByLabelText(/트랙/i),
      "수락(실주문)",
    );
    await userEvent.selectOptions(screen.getByLabelText(/기간/i), "1시간");
    await userEvent.type(screen.getByLabelText(/마크 시점 가격/i), "100");
    await userEvent.click(screen.getByRole("button", { name: /마크 기록/i }));

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
      screen.getByLabelText(/트랙/i),
      "거절 대조",
    );
    await userEvent.selectOptions(screen.getByLabelText(/기간/i), "1시간");
    await userEvent.type(screen.getByLabelText(/마크 시점 가격/i), "100");
    await userEvent.click(screen.getByRole("button", { name: /마크 기록/i }));

    expect(onSubmit).not.toHaveBeenCalled();
    expect(
      screen.getByText(/이 트랙에서는 대조군이 필요합니다/i),
    ).toBeInTheDocument();
  });

  it("offers counterfactual options when one is provided", async () => {
    const cf = makeCounterfactual({
      id: 11,
      track_kind: "rejected_counterfactual",
    });
    render(<OutcomeMarkForm counterfactuals={[cf]} onSubmit={vi.fn()} />);
    await userEvent.selectOptions(
      screen.getByLabelText(/트랙/i),
      "거절 대조",
    );
    expect(screen.getByLabelText(/대조군/i)).toBeInTheDocument();
  });
});

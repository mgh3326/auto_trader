import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import LinkedActionsPanel from "../components/LinkedActionsPanel";
import { makeAction, makeCounterfactual } from "../test/fixtures";

describe("LinkedActionsPanel", () => {
  it("shows live order external_order_id", () => {
    render(
      <LinkedActionsPanel
        actions={[makeAction({ external_order_id: "LIVE-1" })]}
        counterfactuals={[]}
      />,
    );

    expect(screen.getByText("LIVE-1")).toBeInTheDocument();
  });

  it("shows watch alert external_watch_id", () => {
    render(
      <LinkedActionsPanel
        actions={[
          makeAction({
            action_kind: "watch_alert",
            external_order_id: null,
            external_watch_id: "WATCH-1",
          }),
        ]}
        counterfactuals={[]}
      />,
    );

    expect(screen.getByText("WATCH-1")).toBeInTheDocument();
  });

  it("renders empty state", () => {
    render(<LinkedActionsPanel actions={[]} counterfactuals={[]} />);

    expect(screen.getByText("연결된 액션이 없습니다.")).toBeInTheDocument();
  });

  it("renders counterfactuals but no outcomes", () => {
    render(
      <LinkedActionsPanel
        actions={[]}
        counterfactuals={[makeCounterfactual({ baseline_price: "100" })]}
      />,
    );

    expect(screen.getByText("거절 대조")).toBeInTheDocument();
    expect(screen.queryByTestId("outcome-row")).not.toBeInTheDocument();
  });
});

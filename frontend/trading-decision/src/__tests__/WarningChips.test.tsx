import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import WarningChips from "../components/WarningChips";

describe("WarningChips", () => {
  it("renders one chip per known token with friendly text", () => {
    render(
      <WarningChips
        tokens={[
          "missing_quote",
          "stale_quote",
          "missing_orderbook",
          "missing_support_resistance",
          "missing_kr_universe",
          "non_nxt_venue",
          "unknown_venue",
          "unknown_side",
        ]}
      />,
    );
    expect(screen.getByText("Quote missing")).toBeInTheDocument();
    expect(screen.getByText("Quote stale")).toBeInTheDocument();
    expect(screen.getByText("Orderbook missing")).toBeInTheDocument();
    expect(
      screen.getByText("Support / resistance unavailable"),
    ).toBeInTheDocument();
    expect(screen.getByText("KR universe row missing")).toBeInTheDocument();
    expect(screen.getByText("Non-NXT venue")).toBeInTheDocument();
    expect(screen.getByText("Unknown venue")).toBeInTheDocument();
    expect(screen.getByText("Unknown side")).toBeInTheDocument();
  });

  it("renders unknown-but-allowlist-shaped tokens verbatim as text", () => {
    render(<WarningChips tokens={["custom_warning_token"]} />);
    expect(screen.getByText("custom_warning_token")).toBeInTheDocument();
  });

  it("ignores tokens that fail the allowlist", () => {
    render(<WarningChips tokens={["<script>", "Foo Bar"]} />);
    expect(screen.queryByText("<script>")).not.toBeInTheDocument();
    expect(screen.queryByText("Foo Bar")).not.toBeInTheDocument();
  });

  it("returns null when there are no tokens", () => {
    const { container } = render(<WarningChips tokens={[]} />);
    expect(container.firstChild).toBeNull();
  });
});

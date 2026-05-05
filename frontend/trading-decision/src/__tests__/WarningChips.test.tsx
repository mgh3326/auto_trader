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
    expect(screen.getByText("시세 누락")).toBeInTheDocument();
    expect(screen.getByText("시세 오래됨")).toBeInTheDocument();
    expect(screen.getByText("호가 누락")).toBeInTheDocument();
    expect(screen.getByText("지지/저항선 미사용")).toBeInTheDocument();
    expect(screen.getByText("국내 유니버스 누락")).toBeInTheDocument();
    expect(screen.getByText("비-NXT 거래소")).toBeInTheDocument();
    expect(screen.getByText("거래소 알 수 없음")).toBeInTheDocument();
    expect(screen.getByText("방향 알 수 없음")).toBeInTheDocument();
  });

  it("renders unknown-but-allowlist-shaped tokens verbatim as text (with underscore replacement)", () => {
    render(<WarningChips tokens={["custom_warning_token"]} />);
    expect(screen.getByText("custom warning token")).toBeInTheDocument();
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

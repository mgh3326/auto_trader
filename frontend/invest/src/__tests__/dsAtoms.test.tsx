import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Arrow, Button, Card, Hairline, Krw, PL, Pill, Usd } from "../ds";

describe("ds atoms", () => {
  it("Pill renders tone label and applies tone style class", () => {
    render(
      <Pill tone="kis" size="sm">
        KIS
      </Pill>,
    );
    const el = screen.getByText("KIS");
    expect(el.dataset.tone).toBe("kis");
    expect(el.dataset.size).toBe("sm");
  });

  it("PL chooses gain color for positive values", () => {
    render(<PL value={100} pct={1.5} />);
    const el = screen.getByTestId("pl");
    expect(el.dataset.dir).toBe("up");
  });

  it("PL chooses loss color for negative values", () => {
    render(<PL value={-100} pct={-1.5} />);
    expect(screen.getByTestId("pl").dataset.dir).toBe("down");
  });

  it("Krw and Usd render placeholder when value is null", () => {
    render(
      <>
        <Krw v={null} />
        <Usd v={null} />
      </>,
    );
    expect(screen.getAllByText("−").length).toBe(2);
  });

  it("Arrow renders gain glyph for up, loss glyph for down", () => {
    render(
      <>
        <Arrow dir="up" data-testid="up" />
        <Arrow dir="down" data-testid="down" />
      </>,
    );
    expect(screen.getByTestId("up").textContent).toBe("▲");
    expect(screen.getByTestId("down").textContent).toBe("▼");
  });

  it("Card supports soft variant", () => {
    render(
      <Card soft data-testid="c">
        x
      </Card>,
    );
    expect(screen.getByTestId("c").dataset.soft).toBe("true");
  });

  it("Button is keyboard-clickable when not disabled", () => {
    render(<Button>주문</Button>);
    expect(screen.getByRole("button", { name: "주문" })).toBeEnabled();
  });

  it("Hairline renders a 1px divider", () => {
    render(<Hairline data-testid="h" />);
    expect(screen.getByTestId("h")).toBeInTheDocument();
  });
});

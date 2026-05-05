// frontend/trading-decision/src/__tests__/components/NewsRiskHeadlineCard.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import NewsRiskHeadlineCard from "../../components/NewsRiskHeadlineCard";
import { makeNewsRadarItem } from "../../test/fixtures/newsRadar";

describe("NewsRiskHeadlineCard", () => {
  it("renders title, source, severity, and excluded label", () => {
    const item = makeNewsRadarItem({ included_in_briefing: false });
    render(<NewsRiskHeadlineCard item={item} />);
    expect(
      screen.getByRole("link", { name: /UAE airstrike on tanker in Hormuz/i }),
    ).toHaveAttribute("href", item.url);
    expect(screen.getByText(/Reuters/)).toBeInTheDocument();
    expect(screen.getByText("High")).toBeInTheDocument();
    expect(screen.getByText(/collected · not in briefing/i)).toBeInTheDocument();
  });

  it("shows included label when included_in_briefing is true", () => {
    const item = makeNewsRadarItem({
      included_in_briefing: true,
      briefing_reason: null,
    });
    render(<NewsRiskHeadlineCard item={item} />);
    expect(screen.getByText(/in briefing/i)).toBeInTheDocument();
  });

  it("renders themes and matched terms when present", () => {
    const item = makeNewsRadarItem({
      themes: ["oil", "shipping"],
      matched_terms: ["uae", "hormuz"],
    });
    render(<NewsRiskHeadlineCard item={item} />);
    expect(screen.getByText(/oil/)).toBeInTheDocument();
    expect(screen.getByText(/shipping/)).toBeInTheDocument();
    expect(screen.getByText(/uae/)).toBeInTheDocument();
  });
});

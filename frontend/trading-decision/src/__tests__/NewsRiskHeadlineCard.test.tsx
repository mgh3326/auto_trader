// frontend/trading-decision/src/__tests__/NewsRiskHeadlineCard.test.tsx
import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { NewsRiskHeadlineCard } from "../components/NewsRiskHeadlineCard";
import { makeNewsRadarItem } from "../test/fixtures/newsRadar";

describe("NewsRiskHeadlineCard", () => {
  it("renders high severity with danger-themed border and marker", () => {
    const item = makeNewsRadarItem({ severity: "high", title: "WAR" });
    render(<NewsRiskHeadlineCard item={item} />);
    expect(screen.getByText("WAR")).toBeInTheDocument();
    // High severity marker check
    expect(screen.getByText("HIGH SEVERITY")).toBeInTheDocument();
  });

  it("renders theme chips", () => {
    const item = makeNewsRadarItem({ themes: ["oil", "defense"] });
    render(<NewsRiskHeadlineCard item={item} />);
    expect(screen.getByText("oil")).toBeInTheDocument();
    expect(screen.getByText("defense")).toBeInTheDocument();
  });

  it("renders 'IN BRIEFING' badge if included_in_briefing is true", () => {
    const item = makeNewsRadarItem({ included_in_briefing: true });
    render(<NewsRiskHeadlineCard item={item} />);
    expect(screen.getByText(/IN BRIEFING/i)).toBeInTheDocument();
  });
});

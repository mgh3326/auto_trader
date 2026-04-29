import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import StrategyEventTimeline from "../components/StrategyEventTimeline";
import { makeStrategyEvent } from "../test/fixtures";

describe("StrategyEventTimeline", () => {
  it("renders an empty state when there are no events", () => {
    render(<StrategyEventTimeline events={[]} />);
    expect(
      screen.getByText(/no strategy events yet/i),
    ).toBeInTheDocument();
  });

  it("renders event type, severity, confidence, symbols, and timestamp", () => {
    const event = makeStrategyEvent({
      event_type: "operator_market_event",
      source_text: "OpenAI earnings miss",
      normalized_summary: null,
      severity: 4,
      confidence: 75,
      affected_symbols: ["MSFT", "NVDA"],
      affected_markets: ["us"],
      affected_themes: ["ai"],
      created_at: "2026-04-29T01:30:00Z",
    });
    render(<StrategyEventTimeline events={[event]} />);

    expect(
      screen.getByText(/operator_market_event/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/openai earnings miss/i)).toBeInTheDocument();
    expect(screen.getByText(/severity\s*4/i)).toBeInTheDocument();
    expect(screen.getByText(/confidence\s*75/i)).toBeInTheDocument();
    expect(screen.getByText("MSFT")).toBeInTheDocument();
    expect(screen.getByText("NVDA")).toBeInTheDocument();
    expect(screen.getByText(/us/)).toBeInTheDocument();
    expect(screen.getByText(/ai/)).toBeInTheDocument();
  });

  it("prefers normalized_summary over source_text when present", () => {
    const event = makeStrategyEvent({
      source_text: "raw text body",
      normalized_summary: "polished summary",
    });
    render(<StrategyEventTimeline events={[event]} />);
    expect(screen.getByText(/polished summary/i)).toBeInTheDocument();
    expect(screen.queryByText(/raw text body/i)).not.toBeInTheDocument();
  });
});

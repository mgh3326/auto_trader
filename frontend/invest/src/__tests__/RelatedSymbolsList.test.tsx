// frontend/invest/src/__tests__/RelatedSymbolsList.test.tsx
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { RelatedSymbolsList } from "../components/discover/RelatedSymbolsList";

test("renders symbol chips when symbols are present", () => {
  render(<RelatedSymbolsList symbols={[
    { symbol: "AAPL", market: "us", canonical_name: "Apple", mention_count: 3 },
    { symbol: "MSFT", market: "us", canonical_name: "Microsoft", mention_count: 2 },
  ]} />);
  expect(screen.getByText("Apple")).toBeInTheDocument();
  expect(screen.getByText(/AAPL · 3회 언급/)).toBeInTheDocument();
  expect(screen.getByText("Microsoft")).toBeInTheDocument();
});

test("renders prep notice when symbols are empty", () => {
  render(<RelatedSymbolsList symbols={[]} />);
  expect(screen.getByText("관련 종목 분석은 준비 중입니다.")).toBeInTheDocument();
});

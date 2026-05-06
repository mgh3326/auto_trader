// frontend/invest/src/__tests__/RelatedSymbolsList.test.tsx
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { RelatedSymbolsList } from "../components/discover/RelatedSymbolsList";

test("renders symbol chips when symbols are present", () => {
  render(<RelatedSymbolsList symbols={["AAPL", "MSFT"]} />);
  expect(screen.getByText("AAPL")).toBeInTheDocument();
  expect(screen.getByText("MSFT")).toBeInTheDocument();
});

test("renders prep notice when symbols are empty", () => {
  render(<RelatedSymbolsList symbols={[]} />);
  expect(screen.getByText("관련 종목 분석은 준비 중입니다.")).toBeInTheDocument();
});

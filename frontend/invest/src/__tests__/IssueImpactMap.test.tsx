// frontend/invest/src/__tests__/IssueImpactMap.test.tsx
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { IssueImpactMap } from "../components/discover/IssueImpactMap";

test("renders impact pills for known risk_category", () => {
  render(<IssueImpactMap category="geopolitical_oil" />);
  expect(screen.getByText("원유/에너지")).toBeInTheDocument();
  expect(screen.getByText("항공/운송")).toBeInTheDocument();
  expect(screen.getByText("금/방산")).toBeInTheDocument();
});

test("renders fallback notice when category is null", () => {
  render(<IssueImpactMap category={null} />);
  expect(
    screen.getByText("이 이슈에 대한 영향 분석은 준비 중입니다."),
  ).toBeInTheDocument();
});

test("includes disclaimer", () => {
  render(<IssueImpactMap category="macro_policy" />);
  expect(
    screen.getByText("뉴스 기반 참고 정보이며 매매 추천이 아닙니다."),
  ).toBeInTheDocument();
});

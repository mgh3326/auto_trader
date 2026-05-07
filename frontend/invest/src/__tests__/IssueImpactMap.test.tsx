// frontend/invest/src/__tests__/IssueImpactMap.test.tsx
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { IssueImpactMap } from "../components/discover/IssueImpactMap";

test("renders sector impact pills", () => {
  render(<IssueImpactMap direction="up" sectors={["반도체", "AI"]} />);
  expect(screen.getByText("반도체")).toBeInTheDocument();
  expect(screen.getByText("AI")).toBeInTheDocument();
  expect(screen.getAllByText(/긍정 모멘텀/).length).toBeGreaterThanOrEqual(1);
});

test("renders fallback label when sectors are empty", () => {
  render(<IssueImpactMap direction="neutral" sectors={[]} />);
  expect(screen.getByText("관련 시장")).toBeInTheDocument();
});

test("includes disclaimer", () => {
  render(<IssueImpactMap direction="mixed" sectors={["금리"]} />);
  expect(
    screen.getByText("뉴스 기반 참고 정보이며 매매 추천이 아닙니다."),
  ).toBeInTheDocument();
});

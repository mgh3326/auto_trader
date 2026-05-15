import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { ScreenerFreshnessLine } from "../desktop/screener/ScreenerFreshnessLine";

describe("ScreenerFreshnessLine", () => {
  test("renders asOfLabel and relativeLabel separated by '·'", () => {
    render(
      <ScreenerFreshnessLine
        freshness={{
          fetchedAt: "2026-05-10T05:30:00+00:00",
          asOfLabel: "2026.05.10 14:30 기준",
          relativeLabel: "12분 전 갱신",
          cacheHit: false,
          source: "live",
          dataState: "fresh",
        }}
      />,
    );
    expect(screen.getByTestId("screener-freshness")).toHaveTextContent(
      "2026.05.10 14:30 기준 · 12분 전 갱신",
    );
  });

  test("collapses to '전 거래일 기준' when source is previous_session", () => {
    render(
      <ScreenerFreshnessLine
        freshness={{
          fetchedAt: "2026-05-08T06:30:00+00:00",
          asOfLabel: "2026.05.08 15:30 기준",
          relativeLabel: "전 거래일 기준",
          cacheHit: true,
          source: "previous_session",
          dataState: "fresh",
        }}
      />,
    );
    expect(screen.getByTestId("screener-freshness")).toHaveTextContent(
      "전 거래일 기준 · 2026.05.08 15:30 종가",
    );
  });

  test.each([
    ["partial", "일부 데이터"],
    ["stale", "업데이트 필요"],
    ["missing", "데이터 준비중"],
    ["fallback", "대체 데이터"],
  ] as const)("renders %s data-state chip", (dataState, label) => {
    render(
      <ScreenerFreshnessLine
        freshness={{
          fetchedAt: "2026-05-10T05:30:00+00:00",
          asOfLabel: "2026.05.10 14:30 기준",
          relativeLabel: "12분 전 갱신",
          cacheHit: false,
          source: "live",
          dataState,
        }}
      />,
    );
    expect(screen.getByText(label)).toHaveClass(
      `screener-freshness-state--${dataState}`,
    );
  });
});

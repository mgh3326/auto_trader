import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { ScreenerFreshnessLine } from "../desktop/screener/ScreenerFreshnessLine";
import type {
  ScreenerFreshness,
  ScreenerFreshnessPrimary,
  ScreenerFreshnessDependency,
} from "../types/screener";

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

describe("ROB-277 dual-line rendering", () => {
  test("renders 데이터 기준 line and 화면 갱신 line in separate spans", () => {
    render(
      <ScreenerFreshnessLine
        freshness={{
          fetchedAt: "2026-05-13T06:35:00+00:00",
          asOfLabel: "2026.05.13 장마감 기준",
          relativeLabel: "5거래일 지연",
          cacheHit: true,
          source: "cached",
          dataState: "stale",
          servedAt: "2026-05-20T00:10:00+00:00",
          servedRelativeLabel: "방금",
          primary: {
            kind: "screener_snapshot",
            snapshotDate: "2026-05-13",
            computedAt: null,
            asOfLabel: "2026.05.13 장마감 기준",
            dataState: "stale",
            source: "invest_screener_snapshots",
          },
          dependencies: [],
          overallState: "stale",
        }}
      />,
    );
    const dataLine = screen.getByTestId("screener-freshness-data");
    const servedLine = screen.getByTestId("screener-freshness-served");
    expect(dataLine).toHaveTextContent("데이터 기준");
    expect(dataLine).toHaveTextContent("2026.05.13");
    expect(servedLine).toHaveTextContent("화면 갱신");
    expect(servedLine).toHaveTextContent("방금");
    // Crucial non-contradiction checks: stale-aging text never appears on the served line
    expect(servedLine).not.toHaveTextContent("5거래일 지연");
    expect(servedLine).not.toHaveTextContent("업데이트 필요");
    // And served label "방금" never bleeds into the data line
    expect(dataLine).not.toHaveTextContent("방금");
  });

  test("falls back to legacy single-line when primary/served fields absent", () => {
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
    // No new dual-line testids should appear
    expect(screen.queryByTestId("screener-freshness-data")).toBeNull();
    expect(screen.queryByTestId("screener-freshness-served")).toBeNull();
  });
});

describe("ROB-277 follow-up: dependency lag inline on data line", () => {
  test("appends dependency lagLabel to 데이터 기준 line when dependency is stale", () => {
    render(
      <ScreenerFreshnessLine
        freshness={{
          fetchedAt: "2026-05-20T00:10:00+00:00",
          asOfLabel: "2026.05.20 09:10 기준",
          relativeLabel: "방금 갱신",
          cacheHit: true,
          source: "cached",
          dataState: "stale",
          servedAt: "2026-05-20T00:10:00+00:00",
          servedRelativeLabel: "방금",
          primary: {
            kind: "screener_snapshot",
            snapshotDate: "2026-05-20",
            computedAt: "2026-05-20T00:05:00+00:00",
            asOfLabel: "2026.05.20 09:05 기준",
            dataState: "fresh",
            source: "invest_screener_snapshots",
          },
          dependencies: [
            {
              kind: "investor_flow",
              snapshotDate: "2026-05-18",
              collectedAt: "2026-05-18T07:30:00+00:00",
              lagLabel: "2일 지연",
              dataState: "stale",
              source: "investor_flow_snapshots",
            },
          ],
          overallState: "stale",
        }}
      />,
    );
    const dataLine = screen.getByTestId("screener-freshness-data");
    expect(dataLine).toHaveTextContent("데이터 기준 2026.05.20 09:05 기준");
    expect(dataLine).toHaveTextContent("2일 지연");
    // served line stays clean (no lag info)
    const servedLine = screen.getByTestId("screener-freshness-served");
    expect(servedLine).not.toHaveTextContent("지연");
  });

  test("falls back to '업데이트 필요' when dependency is stale without lagLabel", () => {
    render(
      <ScreenerFreshnessLine
        freshness={{
          fetchedAt: "2026-05-20T00:10:00+00:00",
          asOfLabel: "2026.05.20 09:10 기준",
          relativeLabel: "방금 갱신",
          cacheHit: true,
          source: "cached",
          dataState: "stale",
          servedAt: "2026-05-20T00:10:00+00:00",
          servedRelativeLabel: "방금",
          primary: {
            kind: "screener_snapshot",
            snapshotDate: "2026-05-20",
            computedAt: null,
            asOfLabel: "2026.05.20 장마감 기준",
            dataState: "fresh",
            source: "invest_screener_snapshots",
          },
          dependencies: [
            {
              kind: "investor_flow",
              snapshotDate: null,
              collectedAt: null,
              lagLabel: null,
              dataState: "stale",
              source: "investor_flow_snapshots",
            },
          ],
          overallState: "stale",
        }}
      />,
    );
    const dataLine = screen.getByTestId("screener-freshness-data");
    expect(dataLine).toHaveTextContent("업데이트 필요");
  });
});

describe("ScreenerFreshness type shape", () => {
  test("accepts the ROB-277 additive fields", () => {
    const primary: ScreenerFreshnessPrimary = {
      kind: "screener_snapshot",
      snapshotDate: "2026-05-13",
      computedAt: "2026-05-13T06:35:00+00:00",
      asOfLabel: "2026.05.13 장마감 기준",
      dataState: "stale",
      source: "invest_screener_snapshots",
    };
    const dep: ScreenerFreshnessDependency = {
      kind: "investor_flow",
      snapshotDate: "2026-05-18",
      collectedAt: "2026-05-18T07:30:00+00:00",
      lagLabel: "2일 지연",
      dataState: "stale",
      source: "investor_flow_snapshots",
    };
    const f: ScreenerFreshness = {
      fetchedAt: "2026-05-13T06:35:00+00:00",
      asOfLabel: "2026.05.13 장마감 기준",
      relativeLabel: "5거래일 지연",
      cacheHit: true,
      source: "cached",
      dataState: "stale",
      servedAt: "2026-05-20T00:10:00+00:00",
      servedRelativeLabel: "방금",
      primary,
      dependencies: [dep],
      overallState: "stale",
    };
    expect(f.primary?.kind).toBe("screener_snapshot");
    expect(f.dependencies?.[0]?.kind).toBe("investor_flow");
  });
});

describe("ROB-426 degraded partition details", () => {
  test("renders coverageLabel when the primary partition is coverage_below_floor", () => {
    render(
      <ScreenerFreshnessLine
        freshness={{
          fetchedAt: "2026-06-03T06:00:00Z",
          asOfLabel: "2026.05.22 15:30 기준",
          relativeLabel: "방금",
          cacheHit: true,
          source: "cached",
          dataState: "stale",
          primary: {
            kind: "screener_snapshot",
            snapshotDate: "2026-05-22",
            computedAt: null,
            asOfLabel: "2026.05.22 15:30 기준",
            dataState: "stale",
            source: "invest_screener_snapshots",
            degradationReason: "coverage_below_floor",
            coverageLabel: "20 / 3,800 (0.5%)",
          },
          dependencies: [],
          overallState: "stale",
        }}
      />,
    );
    expect(screen.getByText(/20 \/ 3,800 \(0\.5%\)/)).toBeInTheDocument();
  });
});


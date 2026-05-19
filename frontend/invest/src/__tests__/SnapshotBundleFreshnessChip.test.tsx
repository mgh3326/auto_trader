// ROB-269 Phase 4 — SnapshotBundleFreshnessChip render tests.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SnapshotBundleFreshnessChip } from "../components/investment-reports/SnapshotBundleFreshnessChip";

describe("SnapshotBundleFreshnessChip", () => {
  it("returns null when freshnessSummary is null (legacy report)", () => {
    const { container } = render(
      <SnapshotBundleFreshnessChip freshnessSummary={null} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("returns null when freshnessSummary is undefined", () => {
    const { container } = render(
      <SnapshotBundleFreshnessChip freshnessSummary={undefined} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders bundle-level '신선' chip when overall is fresh with no degraded kinds", () => {
    render(
      <SnapshotBundleFreshnessChip
        freshnessSummary={{
          overall: "fresh",
          portfolio: { status: "fresh" },
          journal: { status: "fresh" },
          watch_context: { status: "fresh" },
          market: { status: "fresh" },
        }}
      />,
    );
    const chip = screen.getByTestId("snapshot-bundle-freshness");
    expect(chip).toHaveTextContent("스냅샷 신선");
    expect(chip.className).toContain("snapshot-bundle-freshness--fresh");
    // No degraded per-kind chips should render when all kinds are fresh.
    expect(screen.queryByTestId(/^snapshot-chip-/)).toBeNull();
  });

  it("renders per-critical-kind chips when a critical kind is hard_stale", () => {
    render(
      <SnapshotBundleFreshnessChip
        freshnessSummary={{
          overall: "partial",
          portfolio: { status: "hard_stale" },
          journal: { status: "fresh" },
          watch_context: { status: "fresh" },
          market: { status: "fresh" },
        }}
      />,
    );
    const chip = screen.getByTestId("snapshot-bundle-freshness");
    expect(chip).toHaveTextContent("스냅샷 부분");
    const portfolioChip = screen.getByTestId("snapshot-chip-portfolio");
    expect(portfolioChip).toHaveTextContent("포지션 오래됨");
    expect(portfolioChip.className).toContain(
      "snapshot-bundle-freshness__chip--critical",
    );
    expect(portfolioChip.className).toContain(
      "snapshot-bundle-freshness__chip--hard_stale",
    );
  });

  it("renders optional-kind chips dimmed when news/naver/toss are unavailable", () => {
    render(
      <SnapshotBundleFreshnessChip
        freshnessSummary={{
          overall: "partial",
          portfolio: { status: "fresh" },
          journal: { status: "fresh" },
          watch_context: { status: "fresh" },
          market: { status: "fresh" },
          news: { status: "unavailable" },
          naver_remote_debug: { status: "unavailable" },
          toss_remote_debug: { status: "hard_stale" },
        }}
      />,
    );
    const newsChip = screen.getByTestId("snapshot-chip-news");
    expect(newsChip).toHaveTextContent("뉴스 확인 불가");
    expect(newsChip.className).toContain(
      "snapshot-bundle-freshness__chip--optional",
    );
    expect(screen.getByTestId("snapshot-chip-naver_remote_debug")).toHaveTextContent(
      "네이버 확인 불가",
    );
    expect(screen.getByTestId("snapshot-chip-toss_remote_debug")).toHaveTextContent(
      "토스 오래됨",
    );
    // Critical kinds are all fresh — no critical chips rendered.
    expect(screen.queryByTestId("snapshot-chip-portfolio")).toBeNull();
  });

  it("renders 'failed' overall with red modifier class", () => {
    render(
      <SnapshotBundleFreshnessChip
        freshnessSummary={{
          overall: "failed",
          portfolio: { status: "failed" },
        }}
      />,
    );
    const chip = screen.getByTestId("snapshot-bundle-freshness");
    expect(chip.className).toContain("snapshot-bundle-freshness--failed");
    expect(chip).toHaveTextContent("스냅샷 실패");
  });

  it("renders neutral '확인 불가' when overall is missing/invalid", () => {
    // Defensive path — DB CHECK guards published rows, but draft rows can
    // reach the UI with an unset ``overall``.
    render(
      <SnapshotBundleFreshnessChip
        freshnessSummary={{
          portfolio: { status: "fresh" },
        }}
      />,
    );
    const chip = screen.getByTestId("snapshot-bundle-freshness");
    expect(chip).toHaveTextContent("스냅샷 확인 불가");
    expect(chip.className).toContain("snapshot-bundle-freshness--unavailable");
  });

  it("accepts the bare-status shorthand on per-kind entries", () => {
    // The Python schema allows the per-kind value to be either a dict
    // (``{status: 'fresh', as_of: '...'}``) or just the status string.
    // The chip must accept both.
    render(
      <SnapshotBundleFreshnessChip
        freshnessSummary={{
          overall: "partial",
          portfolio: "hard_stale",
          news: "unavailable",
        }}
      />,
    );
    expect(screen.getByTestId("snapshot-chip-portfolio")).toHaveTextContent(
      "포지션 오래됨",
    );
    expect(screen.getByTestId("snapshot-chip-news")).toHaveTextContent(
      "뉴스 확인 불가",
    );
  });

  it("falls back to raw key when an unknown kind appears", () => {
    render(
      <SnapshotBundleFreshnessChip
        freshnessSummary={{
          overall: "partial",
          some_future_kind: { status: "unavailable" },
        }}
      />,
    );
    expect(screen.getByTestId("snapshot-chip-some_future_kind")).toHaveTextContent(
      "some_future_kind 확인 불가",
    );
  });
});

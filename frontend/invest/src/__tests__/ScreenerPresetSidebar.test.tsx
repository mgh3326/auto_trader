import { render, screen } from "@testing-library/react";
import { test, expect, vi } from "vitest";

import { ScreenerPresetSidebar } from "../desktop/screener/ScreenerPresetSidebar";
import type { ScreenerPreset } from "../types/screener";

function preset(p: Partial<ScreenerPreset> & Pick<ScreenerPreset, "id" | "name">): ScreenerPreset {
  return {
    description: "",
    badges: [],
    filterChips: [],
    metricLabel: "",
    market: "kr",
    ...p,
  };
}

const PRESETS: ScreenerPreset[] = [
  preset({ id: "consecutive_gainers", name: "연속 상승세", presetOrigin: "toss_parity", parityStatus: "full" }),
  preset({
    id: "oversold_recovery",
    name: "저평가 탈출",
    presetOrigin: "toss_parity",
    parityStatus: "mismatch",
    parityNote: "현재 구현은 RSI 기반.",
  }),
  preset({
    id: "kr_high_volume_surge",
    name: "거래량 급증",
    presetOrigin: "auto_trader_original",
  }),
];

test("groups Toss-parity and auto_trader-original presets under separate headings", () => {
  render(<ScreenerPresetSidebar presets={PRESETS} selectedId={null} onSelect={vi.fn()} />);

  expect(screen.getByText("토스증권이 만든")).toBeInTheDocument();
  expect(screen.getByText("auto_trader가 만든")).toBeInTheDocument();
  // All presets still render their selectable button regardless of group.
  expect(screen.getByTestId("screener-preset-consecutive_gainers")).toBeInTheDocument();
  expect(screen.getByTestId("screener-preset-kr_high_volume_surge")).toBeInTheDocument();
});

test("shows a parity marker for mismatch/partial presets only", () => {
  render(<ScreenerPresetSidebar presets={PRESETS} selectedId={null} onSelect={vi.fn()} />);

  // mismatch preset surfaces the "차이" marker; full preset does not.
  expect(screen.getByText("차이")).toBeInTheDocument();
  const full = screen.getByTestId("screener-preset-consecutive_gainers");
  expect(full.textContent).not.toContain("차이");
});

test("omits the auto_trader heading when there are no original presets", () => {
  const tossOnly = PRESETS.filter((p) => p.presetOrigin !== "auto_trader_original");
  render(<ScreenerPresetSidebar presets={tossOnly} selectedId={null} onSelect={vi.fn()} />);

  expect(screen.queryByText("auto_trader가 만든")).not.toBeInTheDocument();
});

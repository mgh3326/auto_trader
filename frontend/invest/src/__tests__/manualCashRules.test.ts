import { describe, expect, it } from "vitest";
import {
  MANUAL_CASH_MAX_KRW,
  changeRatio,
  formatKrwAmount,
  parseKrwInput,
  requiresLargeChangeConfirm,
} from "../manualCashRules";

describe("parseKrwInput", () => {
  it.each([
    ["0", 0],
    ["1", 1],
    [String(MANUAL_CASH_MAX_KRW), MANUAL_CASH_MAX_KRW],
    ["10,000,000,000", MANUAL_CASH_MAX_KRW],
    ["1,234,567", 1_234_567],
    ["  42 ", 42],
  ])("accepts %s", (raw, expected) => {
    expect(parseKrwInput(raw)).toEqual({ ok: true, value: expected });
  });

  it.each([
    String(MANUAL_CASH_MAX_KRW + 1),
    "10,000,000,001",
    "-1",
    "-0",
    "abc",
    "1.5",
    "1.0",
    "1e3",
    "NaN",
    "Infinity",
    "0x10",
    "1,00",
    "1,0000",
    ",100",
    "1 000",
    "",
    "   ",
    "99999999999999999999",
  ])("rejects %s", (raw) => {
    expect(parseKrwInput(raw).ok).toBe(false);
  });
});

describe("requiresLargeChangeConfirm (>50%)", () => {
  it.each([
    [100, 150, false],
    [100, 151, true],
    [100, 50, false],
    [100, 49, true],
    [100, 0, true],
    [100, 100, false],
    [10_000_000, 100_000_000, true],
    [3, 4, false],
    [3, 5, true],
    [null, 1, true],
    [null, 0, false],
    [0, 1, true],
    [0, 0, false],
  ] as const)("current=%s next=%s → %s", (current, next, expected) => {
    expect(requiresLargeChangeConfirm(current, next)).toBe(expected);
  });

  it("has no ratio without a positive baseline", () => {
    expect(changeRatio(null, 10)).toBeNull();
    expect(changeRatio(0, 10)).toBeNull();
    expect(changeRatio(200, 300)).toBe(0.5);
  });
});

it("formats with thousands separators for display only", () => {
  expect(formatKrwAmount(1_234_567)).toBe("1,234,567원");
});

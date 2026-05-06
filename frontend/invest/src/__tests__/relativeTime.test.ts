// frontend/invest/src/__tests__/relativeTime.test.ts
import { expect, test } from "vitest";
import { formatRelativeTime } from "../format/relativeTime";

const NOW = new Date("2026-05-07T12:00:00Z");

test("returns '방금' for under 1 minute", () => {
  expect(formatRelativeTime("2026-05-07T11:59:30Z", NOW)).toBe("방금");
});

test("returns minutes for under 1 hour", () => {
  expect(formatRelativeTime("2026-05-07T11:55:00Z", NOW)).toBe("5분 전");
});

test("returns hours for under 1 day", () => {
  expect(formatRelativeTime("2026-05-07T09:00:00Z", NOW)).toBe("3시간 전");
});

test("returns days for over 24 hours", () => {
  expect(formatRelativeTime("2026-05-05T12:00:00Z", NOW)).toBe("2일 전");
});

test("returns null when input is null", () => {
  expect(formatRelativeTime(null, NOW)).toBeNull();
});

test("returns null when input is in the future", () => {
  expect(formatRelativeTime("2026-05-07T12:30:00Z", NOW)).toBeNull();
});

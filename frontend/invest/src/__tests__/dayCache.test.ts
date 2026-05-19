import { describe, expect, test } from "vitest";
import {
  dayCacheReducer,
  dayDisplayState,
  daysToFetch,
  emptyDayCache,
  enumerateDaysInclusive,
  type CalendarDayPayload,
  type DayCacheState,
} from "../components/calendar/dayCache";

function payload(total: number): CalendarDayPayload {
  return { events: [], clusters: [], total, summary: null };
}

describe("enumerateDaysInclusive", () => {
  test("yields every ISO date in [from, to] inclusive", () => {
    expect(enumerateDaysInclusive("2026-05-17", "2026-05-19")).toEqual([
      "2026-05-17",
      "2026-05-18",
      "2026-05-19",
    ]);
  });

  test("yields a single date when from === to", () => {
    expect(enumerateDaysInclusive("2026-05-19", "2026-05-19")).toEqual([
      "2026-05-19",
    ]);
  });
});

describe("dayCacheReducer", () => {
  test("emptyDayCache has epoch 0 and no entries", () => {
    const s = emptyDayCache();
    expect(s.epoch).toBe(0);
    expect(s.byDate.size).toBe(0);
  });

  test("month-changed bumps epoch but keeps byDate", () => {
    const initial = dayCacheReducer(emptyDayCache(), {
      type: "fetch-succeeded",
      epoch: 0,
      payloadByDate: new Map([["2026-05-19", payload(2)]]),
    });
    const next = dayCacheReducer(initial, { type: "month-changed" });
    expect(next.epoch).toBe(initial.epoch + 1);
    expect(next.byDate.get("2026-05-19")).toEqual(
      expect.objectContaining({ kind: "loaded", total: 2 }),
    );
  });

  test("fetch-started marks unloaded days as loading", () => {
    const next = dayCacheReducer(emptyDayCache(), {
      type: "fetch-started",
      epoch: 0,
      days: ["2026-05-18", "2026-05-19"],
    });
    expect(next.byDate.get("2026-05-18")).toEqual({ kind: "loading" });
    expect(next.byDate.get("2026-05-19")).toEqual({ kind: "loading" });
  });

  test("fetch-started does NOT overwrite loaded or empty days", () => {
    let s: DayCacheState = emptyDayCache();
    s = dayCacheReducer(s, {
      type: "fetch-succeeded",
      epoch: 0,
      payloadByDate: new Map([
        ["2026-05-18", payload(3)],
        ["2026-05-19", payload(0)],
      ]),
    });
    s = dayCacheReducer(s, {
      type: "fetch-started",
      epoch: 0,
      days: ["2026-05-18", "2026-05-19", "2026-05-20"],
    });
    expect(s.byDate.get("2026-05-18")?.kind).toBe("loaded");
    expect(s.byDate.get("2026-05-19")?.kind).toBe("empty");
    expect(s.byDate.get("2026-05-20")?.kind).toBe("loading");
  });

  test("fetch-started ignored when epoch is stale", () => {
    const s = dayCacheReducer(emptyDayCache(), { type: "month-changed" });
    const next = dayCacheReducer(s, {
      type: "fetch-started",
      epoch: 0,
      days: ["2026-05-19"],
    });
    expect(next).toBe(s); // identity unchanged
    expect(next.byDate.get("2026-05-19")).toBeUndefined();
  });

  test("fetch-succeeded sets loaded for total>0, empty for total===0", () => {
    const next = dayCacheReducer(emptyDayCache(), {
      type: "fetch-succeeded",
      epoch: 0,
      payloadByDate: new Map([
        ["2026-05-18", payload(2)],
        ["2026-05-19", payload(0)],
      ]),
    });
    expect(next.byDate.get("2026-05-18")).toEqual(
      expect.objectContaining({ kind: "loaded", total: 2 }),
    );
    expect(next.byDate.get("2026-05-19")).toEqual({ kind: "empty" });
  });

  test("fetch-succeeded ignored when epoch is stale", () => {
    const started = dayCacheReducer(emptyDayCache(), {
      type: "fetch-started",
      epoch: 0,
      days: ["2026-05-19"],
    });
    const bumped = dayCacheReducer(started, { type: "month-changed" });
    const stale = dayCacheReducer(bumped, {
      type: "fetch-succeeded",
      epoch: 0,
      payloadByDate: new Map([["2026-05-19", payload(2)]]),
    });
    // Stale response must not promote loading → loaded.
    expect(stale.byDate.get("2026-05-19")?.kind).toBe("loading");
    expect(stale).toBe(bumped);
  });

  test("fetch-failed marks given days as error (current epoch only)", () => {
    const s = dayCacheReducer(emptyDayCache(), {
      type: "fetch-started",
      epoch: 0,
      days: ["2026-05-19"],
    });
    const next = dayCacheReducer(s, {
      type: "fetch-failed",
      epoch: 0,
      days: ["2026-05-19"],
      reason: "boom",
    });
    expect(next.byDate.get("2026-05-19")).toEqual({
      kind: "error",
      reason: "boom",
    });
  });

  test("fetch-failed ignored when epoch is stale", () => {
    const bumped = dayCacheReducer(emptyDayCache(), { type: "month-changed" });
    const stale = dayCacheReducer(bumped, {
      type: "fetch-failed",
      epoch: 0,
      days: ["2026-05-19"],
      reason: "boom",
    });
    expect(stale).toBe(bumped);
  });
});

describe("daysToFetch", () => {
  test("returns every ISO in [from, to] when cache is empty", () => {
    expect(daysToFetch(emptyDayCache(), "2026-05-17", "2026-05-19")).toEqual([
      "2026-05-17",
      "2026-05-18",
      "2026-05-19",
    ]);
  });

  test("skips loaded, empty, and loading; includes error and unloaded", () => {
    let s = emptyDayCache();
    s = dayCacheReducer(s, {
      type: "fetch-succeeded",
      epoch: 0,
      payloadByDate: new Map([
        ["2026-05-17", payload(2)], // loaded
        ["2026-05-18", payload(0)], // empty
      ]),
    });
    s = dayCacheReducer(s, {
      type: "fetch-started",
      epoch: 0,
      days: ["2026-05-19"],
    });
    s = dayCacheReducer(s, {
      type: "fetch-failed",
      epoch: 0,
      days: ["2026-05-20"],
      reason: "x",
    });
    // 2026-05-21 is unloaded (never touched).
    expect(daysToFetch(s, "2026-05-17", "2026-05-21")).toEqual([
      "2026-05-20",
      "2026-05-21",
    ]);
  });
});

describe("dayDisplayState", () => {
  test("classifies every state for grid count UX (ROB-272 Phase 2)", () => {
    let s = emptyDayCache();
    s = dayCacheReducer(s, {
      type: "fetch-succeeded",
      epoch: 0,
      payloadByDate: new Map([
        ["2026-05-17", payload(3)],
        ["2026-05-18", payload(0)],
      ]),
    });
    s = dayCacheReducer(s, {
      type: "fetch-started",
      epoch: 0,
      days: ["2026-05-19"],
    });
    s = dayCacheReducer(s, {
      type: "fetch-failed",
      epoch: 0,
      days: ["2026-05-20"],
      reason: "x",
    });
    expect(dayDisplayState(s, "2026-05-17")).toBe("loaded-nonzero");
    expect(dayDisplayState(s, "2026-05-18")).toBe("loaded-zero");
    expect(dayDisplayState(s, "2026-05-19")).toBe("loading");
    expect(dayDisplayState(s, "2026-05-20")).toBe("error");
    expect(dayDisplayState(s, "2026-05-21")).toBe("unloaded");
  });
});

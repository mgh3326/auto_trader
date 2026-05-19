import type { CalendarDaySummary } from "../../types/calendar";
import type { CalendarClusterVM, CalendarEventVM } from "./vm";

export interface CalendarDayPayload {
  events: CalendarEventVM[];
  clusters: CalendarClusterVM[];
  total: number;
  summary: CalendarDaySummary | null;
}

export type DayState =
  | { kind: "unloaded" }
  | { kind: "loading" }
  | ({ kind: "loaded" } & CalendarDayPayload)
  | { kind: "empty" }
  | { kind: "error"; reason: string };

export interface DayCacheState {
  /**
   * Monotonic counter bumped on every monthCursor change. Fetch responses that
   * carry an older epoch are dropped — this prevents an in-flight response for
   * a prior month from polluting the new month's state.
   */
  epoch: number;
  byDate: ReadonlyMap<string, DayState>;
}

export type DayCacheAction =
  | { type: "month-changed" }
  | { type: "fetch-started"; epoch: number; days: readonly string[] }
  | {
      type: "fetch-succeeded";
      epoch: number;
      payloadByDate: ReadonlyMap<string, CalendarDayPayload>;
    }
  | {
      type: "fetch-failed";
      epoch: number;
      days: readonly string[];
      reason: string;
    };

export function emptyDayCache(): DayCacheState {
  return { epoch: 0, byDate: new Map() };
}

export function enumerateDaysInclusive(from: string, to: string): string[] {
  const out: string[] = [];
  const start = new Date(`${from}T00:00:00`);
  const end = new Date(`${to}T00:00:00`);
  const cur = new Date(start);
  while (cur.getTime() <= end.getTime()) {
    const y = cur.getFullYear();
    const m = String(cur.getMonth() + 1).padStart(2, "0");
    const d = String(cur.getDate()).padStart(2, "0");
    out.push(`${y}-${m}-${d}`);
    cur.setDate(cur.getDate() + 1);
  }
  return out;
}

export function dayCacheReducer(
  state: DayCacheState,
  action: DayCacheAction,
): DayCacheState {
  switch (action.type) {
    case "month-changed": {
      // Bump epoch (invalidates in-flight) and demote orphaned "loading"
      // entries back to unloaded. The fetches that put them into loading are
      // bound to the previous epoch and will be dropped on arrival, so without
      // this demotion they would stay "loading" forever.
      let next = state.byDate;
      let mutated = false;
      for (const [iso, cur] of state.byDate) {
        if (cur.kind === "loading") {
          if (!mutated) {
            next = new Map(state.byDate);
            mutated = true;
          }
          (next as Map<string, DayState>).delete(iso);
        }
      }
      return { epoch: state.epoch + 1, byDate: next };
    }

    case "fetch-started": {
      if (action.epoch !== state.epoch) return state;
      const next = new Map(state.byDate);
      let changed = false;
      for (const iso of action.days) {
        const cur = next.get(iso);
        // Don't overwrite anything that already holds data or is in flight.
        if (cur && cur.kind !== "unloaded" && cur.kind !== "error") continue;
        next.set(iso, { kind: "loading" });
        changed = true;
      }
      if (!changed) return state;
      return { ...state, byDate: next };
    }

    case "fetch-succeeded": {
      if (action.epoch !== state.epoch) return state;
      const next = new Map(state.byDate);
      for (const [iso, payload] of action.payloadByDate) {
        if (payload.total > 0) {
          next.set(iso, { kind: "loaded", ...payload });
        } else {
          next.set(iso, { kind: "empty" });
        }
      }
      return { ...state, byDate: next };
    }

    case "fetch-failed": {
      if (action.epoch !== state.epoch) return state;
      const next = new Map(state.byDate);
      for (const iso of action.days) {
        next.set(iso, { kind: "error", reason: action.reason });
      }
      return { ...state, byDate: next };
    }
  }
}

/** Days in [from, to] that need a network fetch (unloaded or previously errored). */
export function daysToFetch(
  state: DayCacheState,
  from: string,
  to: string,
): string[] {
  const out: string[] = [];
  for (const iso of enumerateDaysInclusive(from, to)) {
    const cur = state.byDate.get(iso);
    if (!cur || cur.kind === "unloaded" || cur.kind === "error") {
      out.push(iso);
    }
  }
  return out;
}

export type DayDisplayState =
  | "unloaded"
  | "loading"
  | "loaded-zero"
  | "loaded-nonzero"
  | "error";

/**
 * Map raw cache state to a UX-facing label for MonthCalendarGrid count display.
 * Critical invariant for ROB-272 Phase 2: "unloaded" must be distinguishable
 * from "loaded-zero" so a not-yet-fetched day is never rendered as "0 events".
 */
export function dayDisplayState(
  state: DayCacheState,
  iso: string,
): DayDisplayState {
  const cur = state.byDate.get(iso);
  if (!cur || cur.kind === "unloaded") return "unloaded";
  if (cur.kind === "loading") return "loading";
  if (cur.kind === "empty") return "loaded-zero";
  if (cur.kind === "error") return "error";
  return cur.total > 0 ? "loaded-nonzero" : "loaded-zero";
}

import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";
import type { CalendarResponse, CalendarTab } from "../../types/calendar";
import {
  dayCacheReducer,
  daysToFetch,
  emptyDayCache,
  type CalendarDayPayload,
  type DayCacheState,
} from "./dayCache";
import { toClusterVM, toEventVM } from "./vm";

export type CalendarFetchFn = (params: {
  fromDate: string;
  toDate: string;
  tab?: CalendarTab;
}) => Promise<CalendarResponse>;

export interface UseCalendarDayCacheArgs {
  monthCursor: Date;
  selectedDate: string;
  /** Half-width of the initial fetch window around selectedDate. Default 3 → 7 days. */
  initialChunkRadius?: number;
  fetchFn: CalendarFetchFn;
}

export interface UseCalendarDayCacheResult {
  cache: DayCacheState;
  /** Ensure [from, to] is loaded; no-op for days already loaded/empty/in-flight. */
  ensureRange: (from: string, to: string) => void;
}

function fmtIso(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function shiftIso(iso: string, deltaDays: number): string {
  const d = new Date(`${iso}T00:00:00`);
  d.setDate(d.getDate() + deltaDays);
  return fmtIso(d);
}

function buildPayloadMap(
  resp: CalendarResponse,
): Map<string, CalendarDayPayload> {
  const out = new Map<string, CalendarDayPayload>();
  for (const day of resp.days) {
    const events = day.events.map((e) => toEventVM(e, day.date));
    const clusters = day.clusters.map((c) => toClusterVM(c, day.date));
    const total =
      events.length + clusters.reduce((sum, c) => sum + c.count, 0);
    out.set(day.date, {
      events,
      clusters,
      total,
      summary: day.summary ?? null,
    });
  }
  return out;
}

export function useCalendarDayCache(
  args: UseCalendarDayCacheArgs,
): UseCalendarDayCacheResult {
  const { monthCursor, selectedDate, initialChunkRadius = 3, fetchFn } = args;

  const [cache, dispatch] = useReducer(dayCacheReducer, undefined, emptyDayCache);

  // Sync refs: the reducer is the source of truth for visible state, but
  // ensureRange() runs synchronously and may be called twice in the same tick
  // (e.g. observer fires multiple section intersections). The reducer can't
  // dedupe those because dispatched state isn't observable until the next
  // render, so we keep an inflight Set updated synchronously.
  const cacheRef = useRef<DayCacheState>(cache);
  cacheRef.current = cache;
  const inflightRef = useRef<Set<string>>(new Set());

  // Keep latest fetchFn without re-triggering effects when the parent passes
  // a fresh closure each render.
  const fetchFnRef = useRef(fetchFn);
  fetchFnRef.current = fetchFn;

  // Track monthCursor identity changes. Comparing Date instances by reference
  // doesn't work (parent may construct a new Date each render with the same
  // month), so we compare the ISO month-key.
  const monthKey = useMemo(
    () => `${monthCursor.getFullYear()}-${monthCursor.getMonth()}`,
    [monthCursor],
  );
  const prevMonthKeyRef = useRef<string | null>(null);

  // Internal fetch primitive: captures the epoch at call time so a stale
  // response (epoch mismatch) is dropped by the reducer.
  const runFetch = useCallback(
    (from: string, to: string) => {
      const need = daysToFetch(cacheRef.current, from, to).filter(
        (iso) => !inflightRef.current.has(iso),
      );
      if (need.length === 0) return;
      for (const iso of need) inflightRef.current.add(iso);

      const actualFrom = need[0]!;
      const actualTo = need[need.length - 1]!;
      const epoch = cacheRef.current.epoch;
      dispatch({ type: "fetch-started", epoch, days: need });

      fetchFnRef
        .current({ fromDate: actualFrom, toDate: actualTo, tab: "all" })
        .then((resp) => {
          dispatch({
            type: "fetch-succeeded",
            epoch,
            payloadByDate: buildPayloadMap(resp),
          });
        })
        .catch((e: unknown) => {
          dispatch({
            type: "fetch-failed",
            epoch,
            days: need,
            reason: e instanceof Error ? e.message : String(e),
          });
        })
        .finally(() => {
          for (const iso of need) inflightRef.current.delete(iso);
        });
    },
    [],
  );

  // monthCursor change → bump epoch (invalidate in-flight) and clear inflight Set.
  useEffect(() => {
    if (prevMonthKeyRef.current === null) {
      prevMonthKeyRef.current = monthKey;
      return;
    }
    if (prevMonthKeyRef.current !== monthKey) {
      prevMonthKeyRef.current = monthKey;
      inflightRef.current.clear();
      dispatch({ type: "month-changed" });
    }
  }, [monthKey]);

  // Anchor fetch: ±radius around selectedDate. Fires on mount and whenever
  // monthKey changes (page typically updates selectedDate alongside monthCursor
  // — we read the latest selectedDate at the time the effect runs, but do NOT
  // re-fetch on bare selectedDate changes. Clicks within the same month are
  // single-day ensures driven by the page; viewport scrolling is the observer's
  // job. This matches the measurement (warm fixed_overhead ≈ 801ms) — every
  // selectedDate-driven ±3 ensure would be a 2-second hit, which is why the
  // page wires click → ensureRange(iso, iso) instead.
  const selectedDateRef = useRef(selectedDate);
  selectedDateRef.current = selectedDate;
  useEffect(() => {
    const anchor = selectedDateRef.current;
    const from = shiftIso(anchor, -initialChunkRadius);
    const to = shiftIso(anchor, initialChunkRadius);
    runFetch(from, to);
  }, [monthKey, initialChunkRadius, runFetch]);

  const ensureRange = useCallback(
    (from: string, to: string) => {
      runFetch(from, to);
    },
    [runFetch],
  );

  return { cache, ensureRange };
}

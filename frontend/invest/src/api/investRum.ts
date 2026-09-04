export interface InvestRumPayload {
  route: string;
  n_requests: number;
  wall_ms: number;
  slowest: string;
  pending_requests?: number;
  truncated?: true;
}

interface ResourceTimingLike {
  name: string;
  startTime: number;
  responseEnd: number;
}

const INVEST_API_PREFIX = "/invest/api/";
const RUM_COLLECTION_WINDOW_MS = 10_000;
const UUID_SEGMENT = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i;
const NUMERIC_SEGMENT = /^\d+$/;
const SYMBOL_PARENTS = new Set(["symbol", "symbols", "ticker", "tickers", "instrument", "instruments"]);

/**
 * Keep browser RUM tags bounded. This intentionally mirrors
 * ``normalize_invest_rum_path`` in the API router: dynamic IDs and symbols
 * must never become Sentry tag values.
 */
export function normalizeInvestRumPath(path: string): string | null {
  if (!path.startsWith("/")) return null;
  const segments = path.split("/");
  const normalized = segments.map((segment, index) => {
    const parent = segments[index - 1]?.toLowerCase();
    if (UUID_SEGMENT.test(segment)) return ":id";
    if (SYMBOL_PARENTS.has(parent ?? "") && segment) {
      return ":symbol";
    }
    if (NUMERIC_SEGMENT.test(segment)) return ":id";
    return segment;
  });
  return normalized.join("/");
}

// Keep this aligned with lossCutApproval.ts: the API is session-authenticated
// and its POST is protected by the existing CSRF middleware.
function csrfToken(): string | null {
  const csrf = document.cookie
    .split("; ")
    .find((cookie) => cookie.startsWith("csrftoken="))
    ?.split("=")[1];
  return csrf ? decodeURIComponent(csrf) : null;
}

function mutationHeaders(): HeadersInit {
  const csrf = csrfToken();
  return {
    "Content-Type": "application/json",
    ...(csrf ? { "X-CSRFToken": csrf } : {}),
  };
}

function apiPath(name: string): string | null {
  try {
    const path = new URL(name, window.location.origin).pathname;
    return path.startsWith(INVEST_API_PREFIX) ? normalizeInvestRumPath(path) : null;
  } catch {
    return null;
  }
}

export function buildInvestRumPayload(
  route: string,
  routeStartedAt: number,
  entries: readonly ResourceTimingLike[],
): InvestRumPayload {
  const normalizedRoute = normalizeInvestRumPath(route) ?? "/invest";
  const requests = entries
    .map((entry) => ({ entry, path: apiPath(entry.name) }))
    .filter(
      (entry): entry is { entry: ResourceTimingLike; path: string } =>
        entry.path !== null && entry.entry.startTime >= routeStartedAt,
    );
  const lastResponseAt = Math.max(
    routeStartedAt,
    ...requests.map(({ entry }) => entry.responseEnd),
  );
  const slowest = requests.reduce<{ path: string; waitMs: number } | null>(
    (current, { entry, path }) => {
      const waitMs = Math.max(0, entry.responseEnd - entry.startTime);
      return current === null || waitMs > current.waitMs ? { path, waitMs } : current;
    },
    null,
  );
  return {
    route: normalizedRoute,
    n_requests: requests.length,
    wall_ms: Math.max(0, lastResponseAt - routeStartedAt),
    slowest: slowest?.path ?? "/invest/api/none",
  };
}

function beaconPayload(payload: InvestRumPayload): InvestRumPayload & { csrf_token?: string } {
  const csrf = csrfToken();
  return { ...payload, ...(csrf ? { csrf_token: csrf } : {}) };
}

export function postInvestRum(payload: InvestRumPayload): void {
  void fetch("/invest/api/rum", {
    method: "POST",
    credentials: "include",
    headers: mutationHeaders(),
    body: JSON.stringify(payload),
  }).catch(() => undefined);
}

function postInvestRumOnPagehide(payload: InvestRumPayload): void {
  const body = JSON.stringify(beaconPayload(payload));
  const beacon = navigator.sendBeacon;
  if (typeof beacon === "function") {
    const accepted = beacon.call(
      navigator,
      "/invest/api/rum",
      new Blob([body], { type: "application/json" }),
    );
    if (accepted) return;
  }
  void fetch("/invest/api/rum", {
    method: "POST",
    credentials: "include",
    keepalive: true,
    headers: { "Content-Type": "application/json" },
    body,
  }).catch(() => undefined);
}

function fetchApiPath(input: RequestInfo | URL): string | null {
  if (typeof input === "string") return apiPath(input);
  if (input instanceof URL) return apiPath(input.toString());
  return apiPath(input.url);
}

export class InvestRumReporter {
  private entries: ResourceTimingLike[] = [];
  private observer: PerformanceObserver | null = null;
  private route = "/invest";
  private routeStartedAt = 0;
  private collectionWindowTimer: ReturnType<typeof setTimeout> | null = null;
  private settleTimer: ReturnType<typeof setTimeout> | null = null;
  private originalFetch: typeof fetch | null = null;
  private pendingRequests = new Set<number>();
  private nextRequestId = 0;
  private active = false;
  private sent = false;

  private readonly onPagehide = (): void => {
    this.flush(true, true);
  };

  begin(route: string): void {
    this.flush();
    this.observer?.disconnect();
    this.entries = [];
    this.route = route;
    this.routeStartedAt = performance.now();
    this.active = true;
    this.sent = false;
    this.pendingRequests.clear();
    if (typeof PerformanceObserver === "undefined") {
      return;
    }
    this.observer = new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        if (apiPath(entry.name) !== null) {
          this.entries.push(entry as unknown as ResourceTimingLike);
        }
      }
      this.scheduleSettledFlush();
    });
    this.observer.observe({ type: "resource", buffered: true });
    this.installFetchTracker();
    window.removeEventListener("pagehide", this.onPagehide);
    window.addEventListener("pagehide", this.onPagehide, { once: true });
    this.collectionWindowTimer = setTimeout(
      () => this.flush(this.pendingRequests.size > 0),
      RUM_COLLECTION_WINDOW_MS,
    );
  }

  stop(): void {
    this.flush();
    this.active = false;
  }

  private installFetchTracker(): void {
    this.restoreFetch();
    const originalFetch = window.fetch;
    this.originalFetch = originalFetch;
    window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      const path = fetchApiPath(input);
      if (path === null || path === "/invest/api/rum") {
        return originalFetch.call(window, input, init);
      }
      const requestId = this.nextRequestId++;
      this.pendingRequests.add(requestId);
      try {
        return originalFetch.call(window, input, init).finally(() => {
          this.pendingRequests.delete(requestId);
          this.scheduleSettledFlush();
        });
      } catch (error) {
        this.pendingRequests.delete(requestId);
        this.scheduleSettledFlush();
        throw error;
      }
    }) as typeof fetch;
  }

  private restoreFetch(): void {
    if (this.originalFetch !== null) {
      window.fetch = this.originalFetch;
      this.originalFetch = null;
    }
  }

  private scheduleSettledFlush(): void {
    if (!this.active || this.sent || this.pendingRequests.size > 0 || this.entries.length === 0) {
      return;
    }
    if (this.settleTimer !== null) clearTimeout(this.settleTimer);
    this.settleTimer = setTimeout(() => this.flush(), 250);
  }

  private flush(truncated = false, pagehide = false): void {
    if (this.collectionWindowTimer !== null) {
      clearTimeout(this.collectionWindowTimer);
      this.collectionWindowTimer = null;
    }
    if (this.settleTimer !== null) {
      clearTimeout(this.settleTimer);
      this.settleTimer = null;
    }
    if (this.sent || !this.active) {
      return;
    }
    this.sent = true;
    this.observer?.disconnect();
    this.observer = null;
    this.restoreFetch();
    window.removeEventListener("pagehide", this.onPagehide);
    const pendingRequests = this.pendingRequests.size;
    const payload = buildInvestRumPayload(this.route, this.routeStartedAt, this.entries);
    const boundedPayload = truncated
      ? { ...payload, truncated: true as const, pending_requests: pendingRequests }
      : payload;
    if (pagehide) {
      postInvestRumOnPagehide(boundedPayload);
    } else {
      postInvestRum(boundedPayload);
    }
  }
}

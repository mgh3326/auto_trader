export interface InvestRumPayload {
  route: string;
  n_requests: number;
  wall_ms: number;
  slowest: string;
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
function mutationHeaders(): HeadersInit {
  const csrf = document.cookie
    .split("; ")
    .find((cookie) => cookie.startsWith("csrftoken="))
    ?.split("=")[1];
  return {
    "Content-Type": "application/json",
    ...(csrf ? { "X-CSRFToken": decodeURIComponent(csrf) } : {}),
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

export function postInvestRum(payload: InvestRumPayload): void {
  void fetch("/invest/api/rum", {
    method: "POST",
    credentials: "include",
    headers: mutationHeaders(),
    body: JSON.stringify(payload),
  }).catch(() => undefined);
}

export class InvestRumReporter {
  private entries: ResourceTimingLike[] = [];
  private observer: PerformanceObserver | null = null;
  private route = "/invest";
  private routeStartedAt = 0;
  private collectionWindowTimer: ReturnType<typeof setTimeout> | null = null;
  private active = false;
  private sent = false;

  begin(route: string): void {
    this.flush();
    this.observer?.disconnect();
    this.entries = [];
    this.route = route;
    this.routeStartedAt = performance.now();
    this.active = true;
    this.sent = false;
    if (typeof PerformanceObserver === "undefined") {
      return;
    }
    this.observer = new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        if (apiPath(entry.name) !== null) {
          this.entries.push(entry as unknown as ResourceTimingLike);
        }
      }
    });
    this.observer.observe({ type: "resource", buffered: true });
    // Resource timing entries are published only once a request completes and
    // do not expose in-flight requests. Keep the collection window open until
    // its hard cap so a slow fan-out response cannot be omitted behind an
    // earlier completed request. The cap keeps every navigation bounded.
    this.collectionWindowTimer = setTimeout(
      () => this.flush(),
      RUM_COLLECTION_WINDOW_MS,
    );
  }

  stop(): void {
    this.flush();
    this.active = false;
    this.observer?.disconnect();
    this.observer = null;
  }

  private flush(): void {
    if (this.collectionWindowTimer !== null) {
      clearTimeout(this.collectionWindowTimer);
      this.collectionWindowTimer = null;
    }
    if (this.sent || !this.active) {
      return;
    }
    this.sent = true;
    this.observer?.disconnect();
    postInvestRum(buildInvestRumPayload(this.route, this.routeStartedAt, this.entries));
  }
}

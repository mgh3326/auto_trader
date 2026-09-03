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
const RUM_IDLE_FLUSH_MS = 750;

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
    return path.startsWith(INVEST_API_PREFIX) ? path : null;
  } catch {
    return null;
  }
}

export function buildInvestRumPayload(
  route: string,
  routeStartedAt: number,
  entries: readonly ResourceTimingLike[],
): InvestRumPayload {
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
    route,
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
  private flushTimer: ReturnType<typeof setTimeout> | null = null;
  private sent = false;

  begin(route: string): void {
    this.flush();
    this.observer?.disconnect();
    this.entries = [];
    this.route = route;
    this.routeStartedAt = performance.now();
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
      this.scheduleFlush();
    });
    this.observer.observe({ type: "resource", buffered: true });
    this.scheduleFlush();
  }

  stop(): void {
    this.flush();
    this.observer?.disconnect();
    this.observer = null;
  }

  private scheduleFlush(): void {
    if (this.flushTimer !== null) {
      clearTimeout(this.flushTimer);
    }
    this.flushTimer = setTimeout(() => this.flush(), RUM_IDLE_FLUSH_MS);
  }

  private flush(): void {
    if (this.flushTimer !== null) {
      clearTimeout(this.flushTimer);
      this.flushTimer = null;
    }
    if (this.sent || this.routeStartedAt === 0) {
      return;
    }
    this.sent = true;
    this.observer?.disconnect();
    postInvestRum(buildInvestRumPayload(this.route, this.routeStartedAt, this.entries));
  }
}

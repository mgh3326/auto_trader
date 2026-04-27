import { vi } from "vitest";

export interface RecordedCall {
  url: string;
  method: string;
  body?: string;
}

export function mockFetch(
  routes: Record<string, (req: Request) => Response | Promise<Response>>,
): { calls: RecordedCall[] } {
  const calls: RecordedCall[] = [];
  const handler = async (
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    const method = (
      init?.method ??
      (input instanceof Request ? input.method : "GET")
    ).toUpperCase();
    const body = typeof init?.body === "string" ? init.body : undefined;
    calls.push({ url, method, body });

    const parsed = new URL(url, "http://x");
    const route = routes[parsed.pathname + parsed.search] ?? routes[parsed.pathname];
    if (!route) return new Response("no route", { status: 599 });
    return route(new Request(parsed.toString(), init));
  };
  vi.stubGlobal("fetch", handler);
  return { calls };
}

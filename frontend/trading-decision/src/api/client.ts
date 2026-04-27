const API_BASE = "/trading/api";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
    public readonly body: unknown,
  ) {
    super(`API ${status}: ${detail}`);
  }
}

export async function apiFetch<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!res.ok) {
    let body: unknown = null;
    let detail = `${res.status} ${res.statusText}`;
    try {
      body = await res.json();
      detail = extractDetail(body) ?? detail;
    } catch {
      body = null;
    }
    throw new ApiError(res.status, detail, body);
  }
  return (await res.json()) as T;
}

function extractDetail(body: unknown): string | null {
  if (!body || typeof body !== "object" || !("detail" in body)) return null;
  const detail = (body as { detail: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map(String).join(", ");
  return null;
}

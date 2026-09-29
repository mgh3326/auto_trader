import { afterEach, describe, expect, it, vi } from "vitest";
import { saveManualCash } from "../api/manualCash";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("saveManualCash transport", () => {
  it("PUTs JSON with the CSRF header and surfaces 409 detail", async () => {
    document.cookie = "csrftoken=tok123";
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ detail: { error: "confirm_required", message: "m" } }), {
        status: 409,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const payload = { accounts: [{ name: "a", amount: 1 }], expected_updated_at: null, confirm_large_change: false };
    await expect(saveManualCash(payload)).rejects.toMatchObject({ status: 409, error: "confirm_required" });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/invest/api/settings/manual-cash");
    expect(init.method).toBe("PUT");
    expect((init.headers as Record<string, string>)["x-csrftoken"]).toBe("tok123");
    expect(JSON.parse(init.body as string)).toEqual(payload);
  });
});

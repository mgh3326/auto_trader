import { afterEach, describe, expect, it, vi } from "vitest";
import { declareExternalCash, readCsrfCookie } from "../api/fundingAdvisory";
import type { ExternalCashForm } from "../types/fundingAdvisory";

const FORM: ExternalCashForm = {
  owner_user_id: 7,
  location_key: "parking_primary",
  display_label: "파킹통장",
  currency: "KRW",
  amount: "640000",
  as_of: null,
  source_note: "토스증권 → 파킹통장 이동",
  expected_head_declaration_id: null,
  idempotency_key: "funding-ui:test-key",
  requires_exact_operator_confirmed_time: true,
  creates_money_movement: false,
};

afterEach(() => {
  document.cookie = "csrftoken=; Max-Age=0; path=/";
  vi.restoreAllMocks();
});

describe("funding advisory declaration API", () => {
  it("reads the CSRF cookie without exposing another cookie", () => {
    expect(readCsrfCookie("session=secret; csrftoken=signed%3Atoken; theme=dark")).toBe("signed:token");
  });

  it("submits only an explicit timezone-aware declaration with CSRF protection", async () => {
    document.cookie = "csrftoken=signed-token; path=/";
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ declaration_id: "new-row" }), {
        status: 201,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await declareExternalCash(FORM, {
      amount: "640000",
      asOf: "2026-08-15T08:20:00+09:00",
      sourceNote: "토스증권 → 파킹통장 이동",
    });

    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect(init).toMatchObject({
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "x-csrftoken": "signed-token",
      },
    });
    expect(JSON.parse(String(init?.body))).toMatchObject({
      amount: "640000",
      as_of: "2026-08-15T08:20:00+09:00",
      expected_head_declaration_id: null,
      idempotency_key: "funding-ui:test-key",
    });
  });
});

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  declareExternalCash,
  ExternalCashDeclareConflict,
  readCsrfCookie,
} from "../api/fundingAdvisory";
import type { ExternalCashDeclarePayload } from "../types/fundingAdvisory";

const PAYLOAD: ExternalCashDeclarePayload = {
  owner_user_id: 7,
  location_key: "parking_primary",
  display_label: "파킹통장",
  currency: "KRW",
  amount: "0",
  as_of: "2026-08-20T07:30:00+00:00",
  source_note: "운영자 선언",
  expected_head_declaration_id: null,
  idempotency_key: "funding-ui:test-key",
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

    await declareExternalCash(PAYLOAD);

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
      amount: "0",
      as_of: "2026-08-20T07:30:00+00:00",
      expected_head_declaration_id: null,
      idempotency_key: "funding-ui:test-key",
    });
  });

  it("surfaces a 409 current head without treating it as a generic error", async () => {
    document.cookie = "csrftoken=signed-token; path=/";
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: {
            error: "expected_head_conflict",
            message: "expected declaration head does not match current head",
            current_head: {
              declaration_id: "head-2",
              display_label: "파킹통장",
              amount: "1500000",
              currency: "KRW",
              as_of: "2026-08-20T07:31:00+00:00",
            },
          },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    );

    await expect(declareExternalCash(PAYLOAD)).rejects.toMatchObject({
      name: "ExternalCashDeclareConflict",
      error: "expected_head_conflict",
      currentHead: expect.objectContaining({ declaration_id: "head-2" }),
    });
    expect(ExternalCashDeclareConflict).toBeDefined();
  });
});

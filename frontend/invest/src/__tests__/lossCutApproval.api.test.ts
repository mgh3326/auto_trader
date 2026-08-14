import { afterEach, expect, test, vi } from "vitest";

import {
  beginLossCutApproval,
  confirmLossCutApproval,
  fetchLossCutProposalEvidence,
} from "../api/lossCutApproval";

const PROPOSAL_ID = "11111111-2222-4333-8444-555555555555";

afterEach(() => {
  vi.unstubAllGlobals();
  document.cookie = "csrftoken=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
});

test("begin sends an empty body with session and CSRF only", async () => {
  document.cookie = "csrftoken=csrf-fixture; path=/";
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
  vi.stubGlobal("fetch", fetchMock);

  await beginLossCutApproval(PROPOSAL_ID);

  const [url, init] = fetchMock.mock.calls[0]!;
  expect(url).toBe(`/invest/api/loss-cut-approvals/${PROPOSAL_ID}/begin`);
  expect(init).toMatchObject({
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": "csrf-fixture",
    },
  });
  expect(JSON.parse(String(init.body))).toEqual({});
});

test("confirm sends only the opaque ceremony id", async () => {
  document.cookie = "csrftoken=csrf-fixture; path=/";
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
  vi.stubGlobal("fetch", fetchMock);

  await confirmLossCutApproval(PROPOSAL_ID, "c".repeat(48));

  const [url, init] = fetchMock.mock.calls[0]!;
  expect(url).toBe(`/invest/api/loss-cut-approvals/${PROPOSAL_ID}/confirm`);
  expect(JSON.parse(String(init.body))).toEqual({ ceremony_id: "c".repeat(48) });
  expect(String(init.body)).not.toContain("nonce");
  expect(String(init.body)).not.toContain("quantity");
});

test("evidence GET is session-bound and bypasses browser cache", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
  vi.stubGlobal("fetch", fetchMock);

  await fetchLossCutProposalEvidence(PROPOSAL_ID);

  expect(fetchMock).toHaveBeenCalledWith(
    `/invest/api/loss-cut-approvals/${PROPOSAL_ID}`,
    { credentials: "include", signal: undefined, cache: "no-store" },
  );
});

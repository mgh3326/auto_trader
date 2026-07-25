// ROB-885 — safe Linear issue URL builder. Pure helper, no React, no network.
// Returns a validated HTTPS URL string or `null` (caller renders plain text).
import { describe, expect, test } from "vitest";
import { buildLinearIssueUrl } from "../linearLink";

describe("buildLinearIssueUrl", () => {
  test("returns HTTPS issue URL for valid ROB-<number> + configured workspace", () => {
    expect(buildLinearIssueUrl("ROB-885", "https://linear.app/team")).toBe(
      "https://linear.app/team/issue/ROB-885",
    );
  });

  test("preserves existing workspace path segments and normalizes trailing slash", () => {
    expect(buildLinearIssueUrl("ROB-1", "https://linear.app/my-team/")).toBe(
      "https://linear.app/my-team/issue/ROB-1",
    );
    expect(buildLinearIssueUrl("ROB-1", "https://linear.app/a/b/c")).toBe(
      "https://linear.app/a/b/c/issue/ROB-1",
    );
  });

  test("returns null when workspace URL is missing/empty", () => {
    expect(buildLinearIssueUrl("ROB-885", undefined)).toBeNull();
    expect(buildLinearIssueUrl("ROB-885", "")).toBeNull();
    expect(buildLinearIssueUrl("ROB-885", "   ")).toBeNull();
  });

  test("returns null for malformed URL", () => {
    expect(buildLinearIssueUrl("ROB-885", "not-a-url")).toBeNull();
    expect(buildLinearIssueUrl("ROB-885", "://no-host")).toBeNull();
  });

  test("returns null for non-HTTPS protocol", () => {
    expect(buildLinearIssueUrl("ROB-885", "http://linear.app/team")).toBeNull();
    expect(buildLinearIssueUrl("ROB-885", "ftp://linear.app/team")).toBeNull();
    expect(buildLinearIssueUrl("ROB-885", "javascript:void(0)")).toBeNull();
  });

  test("returns null when URL carries credentials", () => {
    expect(
      buildLinearIssueUrl("ROB-885", "https://user:pass@linear.app/team"),
    ).toBeNull();
    expect(buildLinearIssueUrl("ROB-885", "https://user@linear.app/team")).toBeNull();
  });

  test("returns null when base URL includes a query string or hash", () => {
    expect(
      buildLinearIssueUrl("ROB-885", "https://linear.app/team?x=1"),
    ).toBeNull();
    expect(buildLinearIssueUrl("ROB-885", "https://linear.app/team#frag")).toBeNull();
  });

  test("returns null for invalid issue IDs", () => {
    expect(buildLinearIssueUrl("rob-885", "https://linear.app/team")).toBeNull();
    expect(buildLinearIssueUrl("ROB-", "https://linear.app/team")).toBeNull();
    expect(buildLinearIssueUrl("ROB-abc", "https://linear.app/team")).toBeNull();
    expect(buildLinearIssueUrl("ROB885", "https://linear.app/team")).toBeNull();
    expect(buildLinearIssueUrl("", "https://linear.app/team")).toBeNull();
    expect(buildLinearIssueUrl("https://evil.com/", "https://linear.app/team")).toBeNull();
  });

  test("issue ID cannot change host or escape the path (no path traversal)", () => {
    // encodeURIComponent keeps these inert, but verify the result stays under
    // the configured origin and path.
    expect(buildLinearIssueUrl("ROB-1", "https://linear.app/team")).toBe(
      "https://linear.app/team/issue/ROB-1",
    );
    // A URL-shaped issue id is rejected by the ROB-<number> guard, not turned
    // into a link.
    expect(
      buildLinearIssueUrl("ROB-1/../../other", "https://linear.app/team"),
    ).toBeNull();
  });
});

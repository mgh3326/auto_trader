const ISSUE_ID_RE = /^ROB-\d+$/;

// ROB-885 — build a safe HTTPS Linear issue URL, or return null so the caller
// renders the issue id as plain text. Never hardcodes a workspace. The issue
// id is appended via the URL API so it cannot mutate the host or base path.
export function buildLinearIssueUrl(
  issueId: string,
  workspaceUrl: string | undefined,
): string | null {
  if (!issueId || !ISSUE_ID_RE.test(issueId)) return null;
  const raw = workspaceUrl?.trim();
  if (!raw) return null;

  let base: URL;
  try {
    base = new URL(raw);
  } catch {
    return null;
  }

  if (base.protocol !== "https:") return null;
  if (!base.hostname) return null;
  if (base.username || base.password) return null;
  if (base.search) return null;
  if (base.hash) return null;

  const path = base.pathname.replace(/\/+$/, "");
  base.pathname = `${path}/issue/${encodeURIComponent(issueId)}`;
  return base.href;
}

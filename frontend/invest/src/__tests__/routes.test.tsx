// frontend/invest/src/__tests__/routes.test.tsx
import { expect, test } from "vitest";
import { router } from "../routes";

function pathsOf(routes: any[]): string[] {
  const out: string[] = [];
  for (const r of routes) {
    if (r.path) out.push(r.path);
    if (r.children) out.push(...pathsOf(r.children));
  }
  return out;
}

test("router exposes /app/discover and /app/discover/issues/:issueId", () => {
  const paths = pathsOf((router as any).routes);
  expect(paths).toContain("/app/discover");
  expect(paths).toContain("/app/discover/issues/:issueId");
});

test("router still exposes /app and /app/paper", () => {
  const paths = pathsOf((router as any).routes);
  expect(paths).toContain("/app");
  expect(paths).toContain("/app/paper");
  expect(paths).toContain("/app/paper/:variant");
});

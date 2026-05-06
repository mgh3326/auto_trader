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

test("router exposes /discover and /discover/issues/:issueId", () => {
  const paths = pathsOf((router as any).routes);
  expect(paths).toContain("/discover");
  expect(paths).toContain("/discover/issues/:issueId");
});

test("router still exposes / and /paper", () => {
  const paths = pathsOf((router as any).routes);
  expect(paths).toContain("/");
  expect(paths).toContain("/paper");
  expect(paths).toContain("/paper/:variant");
});

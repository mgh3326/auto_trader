import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = resolve(__dirname, "..");
const FORBIDDEN_PATTERNS = [
  /\bdangerouslySetInnerHTML\b/,
  /\binnerHTML\b/,
  /\bplace_order\b/,
  /\bcancel_order\b/,
  /\bmodify_order\b/,
  /\bmanage_watch_alerts\b/,
  /\bpaper_order_handler\b/,
  /\bkis_trading_service\b/,
  /\bfill_notification\b/,
];

function* walk(dir: string): Generator<string> {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const s = statSync(full);
    if (s.isDirectory()) {
      yield* walk(full);
    } else if (/\.(ts|tsx|css)$/.test(entry)) {
      yield full;
    }
  }
}

describe("forbidden mutation imports / unsafe rendering", () => {
  it("no source file uses dangerous HTML or trading-mutation symbols", () => {
    const violations: string[] = [];
    for (const file of walk(SRC)) {
      // Skip this safety test and other tests that legitimately mention forbidden
      // tokens as string literals in assertions (e.g. bundle-content checks).
      if (file.endsWith("forbidden_mutation_imports.test.ts")) continue;
      if (file.endsWith("api.decisions.test.ts")) continue;
      const content = readFileSync(file, "utf8");
      for (const re of FORBIDDEN_PATTERNS) {
        if (re.test(content)) violations.push(`${file}: ${re}`);
      }
    }
    expect(violations).toEqual([]);
  });
});

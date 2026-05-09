import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const TOKENS_CSS = readFileSync(resolve(here, "../styles/tokens.css"), "utf8");
const INDEX_HTML = readFileSync(resolve(here, "../../index.html"), "utf8");

// Pull the body of the *first* CSS rule whose selector starts with `selector`.
// We scan brace-by-brace so nested at-rules (`@media (...) { :root:not(...) {} }`)
// don't trip up a naive `indexOf("}")` search.
function ruleBody(css: string, selector: string): string {
  const startIdx = css.indexOf(selector);
  if (startIdx < 0) throw new Error(`selector not found: ${selector}`);
  let i = css.indexOf("{", startIdx);
  if (i < 0) throw new Error(`opening brace not found after: ${selector}`);
  const bodyStart = i + 1;
  let depth = 1;
  i = bodyStart;
  while (i < css.length && depth > 0) {
    const ch = css[i];
    if (ch === "{") depth += 1;
    else if (ch === "}") depth -= 1;
    if (depth === 0) break;
    i += 1;
  }
  if (depth !== 0) throw new Error(`unbalanced braces in: ${selector}`);
  return css.slice(bodyStart, i);
}

const DARK_REQUIRED_TOKENS = [
  "--bg",
  "--bg-alt",
  "--surface",
  "--surface-2",
  "--surface-3",
  "--fg",
  "--fg-1",
  "--fg-2",
  "--divider",
  "--border",
  "--gain",
  "--loss",
  "--overlay",
  "--ai-card-bg",
] as const;

describe("tokens.css contract", () => {
  it("declares the [data-theme=\"dark\"] selector", () => {
    expect(TOKENS_CSS).toMatch(/\[data-theme="dark"\]\s*\{/);
  });

  it("defines every required dark token under [data-theme=\"dark\"]", () => {
    const body = ruleBody(TOKENS_CSS, '[data-theme="dark"]');
    for (const token of DARK_REQUIRED_TOKENS) {
      // `--token: …;` — must have a non-empty value.
      const re = new RegExp(`${token.replace(/[-\\^$*+?.()|[\]{}]/g, "\\$&")}\\s*:\\s*[^;]+;`);
      expect(body, `missing ${token} in [data-theme="dark"]`).toMatch(re);
    }
  });

  it("preserves Korean P/L semantics (gain ≠ loss, loss ≠ accent) under dark", () => {
    const body = ruleBody(TOKENS_CSS, '[data-theme="dark"]');
    const valueOf = (token: string): string => {
      const re = new RegExp(`${token}\\s*:\\s*([^;]+);`);
      const m = body.match(re);
      if (!m || m[1] == null) throw new Error(`token not found: ${token}`);
      return m[1].trim();
    };
    const gain = valueOf("--gain");
    const loss = valueOf("--loss");
    const accent = valueOf("--accent");
    // ▲ red gain, ▼ blue loss, accent stays a separate brand blue.
    expect(gain).not.toEqual(loss);
    expect(loss).not.toEqual(accent);
  });

  it("keeps the light :root tokens intact (no dark overwrite at root)", () => {
    const body = ruleBody(TOKENS_CSS, ":root");
    expect(body).toMatch(/--bg\s*:\s*#ffffff/);
    expect(body).toMatch(/--fg\s*:\s*#191f28/);
    // Korean P/L stays red gain / blue loss in light.
    expect(body).toMatch(/--gain\s*:\s*#f04452/);
    expect(body).toMatch(/--loss\s*:\s*#2a6df4/);
  });

  it("OS dark fallback yields to explicit data-theme=\"light\"", () => {
    // The fallback selector must exclude both data-theme="light" and "dark"
    // so an explicit theme attribute always wins over OS preference.
    expect(TOKENS_CSS).toMatch(
      /@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root:not\(\[data-theme="light"\]\):not\(\[data-theme="dark"\]\)/,
    );
  });

  it("does not leave hardcoded card whites in the global stylesheet itself", () => {
    // Allowlist: token-file is the only place where #ffffff may legitimately
    // appear (as the light --bg / --surface value). Keep it out of components.
    const componentHits = TOKENS_CSS.match(/#ffffff/gi) ?? [];
    // We expect at most a handful (light --bg, --surface, --fg-on-accent,
    // --accent-fg, dark --fg-on-accent, --accent-fg = 6).
    expect(componentHits.length).toBeLessThanOrEqual(8);
  });

  it("keeps index.html body color themeable", () => {
    // Body can set layout-only styles, but themed color/background must come
    // from tokens.css so data-theme="light" and future toggles are not
    // overridden by inline dark first-paint styles.
    const bodyTag = INDEX_HTML.match(/<body\b[^>]*>/i)?.[0] ?? "";
    expect(bodyTag).not.toMatch(/background\s*:/i);
    expect(bodyTag).not.toMatch(/color\s*:/i);
  });
});

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { MobileShell } from "../mobile/MobileShell";

const here = dirname(fileURLToPath(import.meta.url));
const STYLES_CSS = readFileSync(resolve(here, "../styles.css"), "utf8");

function ruleBody(css: string, selector: string): string {
  const start = css.indexOf(selector + " {");
  if (start < 0) throw new Error(`selector not found: ${selector}`);
  const open = css.indexOf("{", start);
  const close = css.indexOf("}", open);
  return css.slice(open + 1, close);
}

function LongChild() {
  return (
    <div data-testid="long-content">
      {Array.from({ length: 200 }).map((_, i) => (
        <p key={i} style={{ height: 20 }}>row {i}</p>
      ))}
    </div>
  );
}

function renderShell() {
  return render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/"]}>
      <MobileShell title="홈">
        <LongChild />
      </MobileShell>
    </MemoryRouter>,
  );
}

describe("MobileShell layout contract", () => {
  it("places the bottom nav as a sibling of the scroll region (never inside it)", () => {
    renderShell();
    const shell = screen.getByTestId("mobile-shell");
    const scroll = screen.getByTestId("mobile-shell-scroll");
    const nav = screen.getByTestId("mobile-bottom-nav");

    // Both scroll region and bottom nav are direct children of the shell.
    expect(scroll.parentElement).toBe(shell);
    expect(nav.parentElement).toBe(shell);
    // Long content is rendered inside the scroll region, not below the nav.
    expect(scroll.contains(screen.getByTestId("long-content"))).toBe(true);
    // Nav is a sibling that follows the scroll region in DOM order.
    expect(scroll.contains(nav)).toBe(false);
    expect(scroll.compareDocumentPosition(nav) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("uses the .mobile-shell + .mobile-shell__scroll classes", () => {
    renderShell();
    expect(screen.getByTestId("mobile-shell").className).toBe("mobile-shell");
    expect(screen.getByTestId("mobile-shell-scroll").className).toBe("mobile-shell__scroll");
  });
});

describe("MobileShell CSS contract", () => {
  it(".mobile-shell pins height to viewport (100vh fallback + 100dvh) and clips overflow", () => {
    const body = ruleBody(STYLES_CSS, ".mobile-shell");
    expect(body).toMatch(/height:\s*100vh/);
    expect(body).toMatch(/height:\s*100dvh/);
    expect(body).toMatch(/max-height:\s*100vh/);
    expect(body).toMatch(/max-height:\s*100dvh/);
    expect(body).toMatch(/display:\s*flex/);
    expect(body).toMatch(/flex-direction:\s*column/);
    expect(body).toMatch(/overflow:\s*hidden/);
  });

  it(".mobile-shell__scroll uses flex: 1 + min-height: 0 + overflow-y: auto", () => {
    const body = ruleBody(STYLES_CSS, ".mobile-shell__scroll");
    expect(body).toMatch(/flex:\s*1/);
    expect(body).toMatch(/min-height:\s*0/);
    expect(body).toMatch(/overflow-y:\s*auto/);
  });

  it("MobileBottomNav uses .mobile-bottom-nav with safe-area-inset-bottom padding", () => {
    renderShell();
    const nav = screen.getByTestId("mobile-bottom-nav");
    expect(nav.className).toContain("mobile-bottom-nav");
    // jsdom doesn't apply CSS files, so we assert on the rule itself.
    const body = ruleBody(STYLES_CSS, ".mobile-bottom-nav");
    expect(body).toMatch(/padding-bottom:\s*max\(8px,\s*env\(safe-area-inset-bottom/);
    // flexShrink: 0 still inline (atomic, jsdom-safe).
    expect(nav.getAttribute("style") ?? "").toContain("flex-shrink: 0");
  });
});

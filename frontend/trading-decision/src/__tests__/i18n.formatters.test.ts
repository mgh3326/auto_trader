import { describe, expect, it } from "vitest";
import {
  labelOperatorToken,
  labelOrToken,
  labelOrderSide,
} from "../i18n/formatters";

describe("labelOrToken", () => {
  it("returns the Korean label when the key is known", () => {
    const map = { open: "진행 중", closed: "종료" } as const;
    expect(labelOrToken(map, "open")).toBe("진행 중");
  });

  it("falls back to the formatted token when the key is unknown", () => {
    const map: Record<string, string> = { open: "진행 중" };
    expect(labelOrToken(map, "needs_review")).toBe("needs review");
  });

  it("returns the dash placeholder for null/undefined", () => {
    const map = { open: "진행 중" } as const;
    expect(labelOrToken(map, null)).toBe("—");
    expect(labelOrToken(map, undefined)).toBe("—");
  });
});

describe("labelOrderSide", () => {
  it("translates buy/sell", () => {
    expect(labelOrderSide("buy")).toBe("매수");
    expect(labelOrderSide("sell")).toBe("매도");
  });

  it("returns dash for none/null", () => {
    expect(labelOrderSide("none")).toBe("—");
    expect(labelOrderSide(null)).toBe("—");
  });
});

describe("labelOperatorToken", () => {
  it("converts snake_case tokens to spaced text", () => {
    expect(labelOperatorToken("paper_plumbing_smoke")).toBe(
      "paper plumbing smoke",
    );
  });

  it("returns dash for null/empty", () => {
    expect(labelOperatorToken(null)).toBe("—");
    expect(labelOperatorToken("")).toBe("—");
  });
});

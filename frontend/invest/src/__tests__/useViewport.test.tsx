import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { useViewport } from "../hooks/useViewport";

function setWidth(w: number) {
  Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: w });
}

afterEach(() => setWidth(1280));

describe("useViewport", () => {
  it("returns 'mobile' under 900px", () => {
    setWidth(600);
    const { result } = renderHook(() => useViewport());
    expect(result.current).toBe("mobile");
  });

  it("returns 'compact' between 900 and 1199", () => {
    setWidth(1024);
    const { result } = renderHook(() => useViewport());
    expect(result.current).toBe("compact");
  });

  it("returns 'desktop' at 1200 and above", () => {
    setWidth(1280);
    const { result } = renderHook(() => useViewport());
    expect(result.current).toBe("desktop");
  });

  it("updates when window resizes", () => {
    setWidth(1280);
    const { result } = renderHook(() => useViewport());
    expect(result.current).toBe("desktop");
    act(() => {
      setWidth(500);
      window.dispatchEvent(new Event("resize"));
    });
    expect(result.current).toBe("mobile");
  });
});

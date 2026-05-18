import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Node 25 ships a partial built-in globalThis.localStorage that lacks
// Storage.prototype methods like clear()/getItem/setItem and shadows jsdom's
// implementation. Install a minimal Storage-compatible polyfill so tests can
// call the full API.
function createMemoryStorage(): Storage {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    clear() {
      map.clear();
    },
    getItem(key: string) {
      return map.has(key) ? (map.get(key) as string) : null;
    },
    key(index: number) {
      return Array.from(map.keys())[index] ?? null;
    },
    removeItem(key: string) {
      map.delete(key);
    },
    setItem(key: string, value: string) {
      map.set(key, String(value));
    },
  };
}

for (const name of ["localStorage", "sessionStorage"] as const) {
  const storage = createMemoryStorage();
  Object.defineProperty(globalThis, name, { configurable: true, value: storage });
  if (typeof window !== "undefined") {
    Object.defineProperty(window, name, { configurable: true, value: storage });
  }
}

afterEach(() => cleanup());

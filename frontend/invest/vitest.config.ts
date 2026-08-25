import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    environmentOptions: { jsdom: { url: "http://localhost/" } },
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: true,
    // Not scoped to src/__tests__/ only: a co-located test (React convention
    // -- Component.test.tsx next to Component.tsx) placed anywhere under
    // src/ must still be collected. *.spec.* is included defensively even
    // though the repo currently has none, since it is vitest's own default
    // include pattern's other half.
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    // Explicit rather than relying on vitest's default (false): a future
    // change flipping this to true would silently let a zero-collection run
    // exit green again.
    passWithNoTests: false,
  },
});

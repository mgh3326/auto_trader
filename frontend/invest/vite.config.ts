import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // Shared base for both pages: trader.html's module/asset URLs resolve under
  // /invest/app/assets/ and are served by the existing invest_app_spa asset
  // route from the same dist/ directory.
  base: "/invest/app/",
  build: {
    outDir: "dist",
    assetsDir: "assets",
    sourcemap: true,
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: fileURLToPath(new URL("./index.html", import.meta.url)),
        trader: fileURLToPath(new URL("./trader.html", import.meta.url)),
      },
    },
  },
  server: {
    port: 5174,
    strictPort: true,
    proxy: {
      "/invest/api": { target: "http://localhost:8000", changeOrigin: false },
      "/portfolio/api": { target: "http://localhost:8000", changeOrigin: false },
      "/trading/api": { target: "http://localhost:8000", changeOrigin: false },
      "/api": { target: "http://localhost:8000", changeOrigin: false },
      "/auth": { target: "http://localhost:8000", changeOrigin: false },
    },
  },
});

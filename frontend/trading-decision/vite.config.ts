import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/trading/decisions/",
  build: {
    outDir: "dist",
    assetsDir: "assets",
    sourcemap: true,
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/trading/api": { target: "http://localhost:8000", changeOrigin: false },
      "/auth": { target: "http://localhost:8000", changeOrigin: false },
      "/api": { target: "http://localhost:8000", changeOrigin: false },
    },
  },
});

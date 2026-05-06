import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/invest/app/",
  build: { outDir: "dist", assetsDir: "assets", sourcemap: true, emptyOutDir: true },
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

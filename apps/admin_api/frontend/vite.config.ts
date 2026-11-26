/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// apps/admin_api/frontend's Vite config. `base: "./"` (relative, not "/") since
// apps/admin_api/main.py serves the built `dist/` from a StaticFiles
// mount, not necessarily at the domain root once an Ingress adds a
// path prefix — a relative base keeps the built `index.html`'s own
// asset references correct regardless of mount path.
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: {
    outDir: "dist",
    sourcemap: true,
  },
  server: {
    // Local `vite dev` proxies API calls to a real apps/admin_api process
    // running on its own Compose port (docker-compose.yml's
    // ADMIN_API_PORT, default 8001) — only used for the `npm run dev` inner
    // loop; the built `dist/` (same-origin, no proxy) is what actually
    // ships.
    proxy: {
      "/api": "http://localhost:8001",
      "/healthz": "http://localhost:8001",
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});

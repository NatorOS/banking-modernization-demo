import { fileURLToPath, URL } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The build is inlined into one index.html by scripts/inline-build.mjs, so keep it to one JS and
// one CSS file with fonts embedded.
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  build: { assetsInlineLimit: 1024 * 1024, cssCodeSplit: false },
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
});

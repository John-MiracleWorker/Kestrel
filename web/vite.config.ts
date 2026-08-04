import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  // Relative asset base so the built renderer also boots from file:// inside
  // the installed Electron shell (and the e2e harness that drives web/dist
  // directly) without a dev server.
  base: "./",
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8765"
    }
  }
});

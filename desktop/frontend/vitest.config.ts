import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Vitest config kept separate from the production Vite build config so test
// tooling/globals never leak into the shipped bundle. Test files live next to
// the code as ``*.test.ts(x)`` and under ``src/test/``; the production
// ``tsconfig.json`` excludes them and ``vite build`` never imports them, so
// they are not part of the packaged frontend.
export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: true,
  },
});

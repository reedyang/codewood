import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
// base: "./" makes asset URLs relative so the bundle can be loaded from a
// file:// URL inside the packaged WebView host.
export default defineConfig({
    base: "./",
    plugins: [react()],
    build: {
        outDir: "dist",
        emptyOutDir: true,
    },
});

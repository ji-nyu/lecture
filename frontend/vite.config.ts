import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The browser calls `/api/*`; Vite forwards it to the FastAPI backend and
// strips the `/api` prefix (backend routes live at the root, e.g. /projects).
// `npm run ui` uses mode `ui-preview`: fake data only, no backend proxy.
export default defineConfig(({ mode }) => {
  const uiPreview = mode === "ui-preview";
  return {
    plugins: [react()],
    server: {
      port: uiPreview ? 5174 : 5173,
      strictPort: uiPreview,
      proxy: uiPreview
        ? undefined
        : {
            "/api": {
              target: "http://127.0.0.1:8000",
              changeOrigin: true,
              rewrite: (path) => path.replace(/^\/api/, ""),
            },
          },
    },
  };
});

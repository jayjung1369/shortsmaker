import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: process.env.VITE_BASE_PATH || "/",
  server: {
    port: 5174,
    proxy: { "/api": "http://localhost:8000" },
  },
});
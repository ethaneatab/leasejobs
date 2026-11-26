import { defineConfig } from "@playwright/test";

// Needs ./scripts/demo.sh running (API :8910, workers, scheduler, web :5191).
export default defineConfig({
  testDir: ".",
  timeout: 90_000,
  workers: 1, // the learn flow and the kill button share one set of workers
  reporter: [["list"]],
  use: { baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:5191", headless: true, viewport: { width: 1400, height: 900 } },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});

import { defineConfig } from "@playwright/test";

const existingServer = process.env.PLAYWRIGHT_BASE_URL;
const baseURL = existingServer ?? "http://localhost:3000";

export default defineConfig({
  testDir: "./e2e",
  use: { baseURL, browserName: "chromium", headless: true },
  webServer: existingServer ? undefined : { command: "npm run dev -- --port 3000", url: baseURL, reuseExistingServer: true, timeout: 60_000 },
});

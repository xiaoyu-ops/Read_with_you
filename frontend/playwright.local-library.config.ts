import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { defineConfig } from "playwright/test";

const port = 3223;
const baseURL = `http://127.0.0.1:${port}`;
const macChrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

export default defineConfig({
  testDir: "./tests",
  testMatch: "localPaperLibrary.browser.spec.ts",
  fullyParallel: false,
  workers: 1,
  timeout: 120_000,
  expect: { timeout: 12_000 },
  reporter: "line",
  outputDir: path.join(os.tmpdir(), "pet-local-library-playwright"),
  webServer: {
    command: `npm run dev -- --hostname 127.0.0.1 --port ${port}`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      NEXT_DIST_DIR: ".next-local-library-playwright",
      NEXT_PUBLIC_API_BASE: "/api-mock",
    },
  },
  use: {
    baseURL,
    headless: true,
    viewport: { width: 1066, height: 900 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    launchOptions: fs.existsSync(macChrome) ? { executablePath: macChrome } : {},
  },
});

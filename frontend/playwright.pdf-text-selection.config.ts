import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { defineConfig } from "playwright/test";


const macChrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const port = 3218;
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: "./tests",
  testMatch: "pdfTextSelection.browser.spec.ts",
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  reporter: "line",
  outputDir: path.join(os.tmpdir(), "pet-pdf-selection-playwright"),
  webServer: {
    command: `python3 -m http.server ${port} --bind 127.0.0.1`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 30_000,
  },
  use: {
    baseURL,
    headless: true,
    launchOptions: fs.existsSync(macChrome) ? { executablePath: macChrome } : {},
  },
});

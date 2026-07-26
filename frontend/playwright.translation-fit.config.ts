import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { defineConfig } from "playwright/test";


const macChrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

export default defineConfig({
  testDir: "./tests",
  testMatch: "translationFit.browser.spec.ts",
  fullyParallel: false,
  workers: 1,
  timeout: 90_000,
  reporter: "line",
  outputDir: path.join(os.tmpdir(), "pet-translation-fit-playwright"),
  use: {
    headless: true,
    launchOptions: fs.existsSync(macChrome) ? { executablePath: macChrome } : {},
  },
});

import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 10_000 },
  globalSetup: "./support/global-setup.ts",
  outputDir: "./test-results",
  use: {
    trace: "off",
  },
  projects: [
    { name: "quick", grep: /@(A|B|C|D)\b/ },
    { name: "full", grep: /@[A-E]\b/ },
  ],
});

import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./support-tests",
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  use: {
    trace: "off",
  },
});

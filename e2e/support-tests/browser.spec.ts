import { test, expect } from "@playwright/test";

import { launchBrowser } from "../support/browser";

function fakePlaywright(launch: (options?: unknown) => Promise<unknown>) {
  return { chromium: { launch } } as never;
}

test("browser defaults to the installed Microsoft Edge channel", async () => {
  const calls: unknown[] = [];
  const browser = { marker: "edge" };
  const original = process.env.E2E_BROWSER;
  delete process.env.E2E_BROWSER;
  try {
    await expect(
      launchBrowser(
        fakePlaywright(async (options) => {
          calls.push(options);
          return browser;
        }),
      ),
    ).resolves.toBe(browser);
    expect(calls).toEqual([{ channel: "msedge" }]);
  } finally {
    if (original === undefined) delete process.env.E2E_BROWSER;
    else process.env.E2E_BROWSER = original;
  }
});

test("browser edge mode fails clearly and never silently falls back", async () => {
  const calls: unknown[] = [];
  const original = process.env.E2E_BROWSER;
  process.env.E2E_BROWSER = "edge";
  try {
    await expect(
      launchBrowser(
        fakePlaywright(async (options) => {
          calls.push(options);
          throw new Error("executable does not exist");
        }),
      ),
    ).rejects.toThrow(
      "未找到 Microsoft Edge；请安装 Edge，或设置 E2E_BROWSER=chromium 并运行 playwright install chromium",
    );
    expect(calls).toEqual([{ channel: "msedge" }]);
  } finally {
    if (original === undefined) delete process.env.E2E_BROWSER;
    else process.env.E2E_BROWSER = original;
  }
});

test("browser chromium mode uses Playwright Chromium without Edge options", async () => {
  const calls: unknown[] = [];
  const browser = { marker: "chromium" };
  const original = process.env.E2E_BROWSER;
  process.env.E2E_BROWSER = "chromium";
  try {
    await expect(
      launchBrowser(
        fakePlaywright(async (options) => {
          calls.push(options);
          return browser;
        }),
      ),
    ).resolves.toBe(browser);
    expect(calls).toEqual([undefined]);
  } finally {
    if (original === undefined) delete process.env.E2E_BROWSER;
    else process.env.E2E_BROWSER = original;
  }
});

test("browser rejects unsupported selectors", async () => {
  const original = process.env.E2E_BROWSER;
  process.env.E2E_BROWSER = "firefox";
  try {
    await expect(launchBrowser(fakePlaywright(async () => ({})))).rejects.toThrow(
      "E2E_BROWSER 只支持 edge 或 chromium",
    );
  } finally {
    if (original === undefined) delete process.env.E2E_BROWSER;
    else process.env.E2E_BROWSER = original;
  }
});

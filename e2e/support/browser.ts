import type { Browser, Playwright } from "@playwright/test";

export async function launchBrowser(playwright: Playwright): Promise<Browser> {
  const selection = process.env.E2E_BROWSER || "edge";
  if (selection === "chromium") {
    return await playwright.chromium.launch();
  }
  if (selection === "edge") {
    try {
      return await playwright.chromium.launch({ channel: "msedge" });
    } catch (error) {
      throw new Error(
        "未找到 Microsoft Edge；请安装 Edge，或设置 E2E_BROWSER=chromium 并运行 playwright install chromium",
        { cause: error },
      );
    }
  }
  throw new Error("E2E_BROWSER 只支持 edge 或 chromium");
}

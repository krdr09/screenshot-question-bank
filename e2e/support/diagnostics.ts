import type { Page } from "@playwright/test";

export interface PageDiagnostics {
  consoleMessages: string[];
  pageErrors: string[];
  requestFailures: string[];
}

export function attachPageDiagnostics(
  page: Page,
  diagnostics: PageDiagnostics = {
    consoleMessages: [],
    pageErrors: [],
    requestFailures: [],
  },
): PageDiagnostics {
  page.on("console", (message) => {
    diagnostics.consoleMessages.push(`[${message.type()}] ${message.text()}`);
  });
  page.on("pageerror", (error) => {
    diagnostics.pageErrors.push(String(error));
  });
  page.on("requestfailed", (request) => {
    diagnostics.requestFailures.push(
      `${request.method()} ${request.url()} ${request.failure()?.errorText ?? "unknown failure"}`,
    );
  });
  return diagnostics;
}

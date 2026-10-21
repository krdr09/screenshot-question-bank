import { EventEmitter } from "node:events";

import { test, expect } from "@playwright/test";

import { attachPageDiagnostics } from "../support/diagnostics";

test("diagnostics captures console pageerror and failed requests without response bodies", () => {
  const page = new EventEmitter();
  const diagnostics = attachPageDiagnostics(page as never);

  page.emit("console", { type: () => "warning", text: () => "console detail" });
  page.emit("pageerror", new Error("page exploded"));
  page.emit("requestfailed", {
    method: () => "POST",
    url: () => "http://127.0.0.1/api/imports",
    failure: () => ({ errorText: "net::ERR_CONNECTION_RESET" }),
    response: () => ({ body: () => "must-not-be-read" }),
  });

  expect(diagnostics.consoleMessages).toEqual(["[warning] console detail"]);
  expect(diagnostics.pageErrors).toEqual(["Error: page exploded"]);
  expect(diagnostics.requestFailures).toEqual([
    "POST http://127.0.0.1/api/imports net::ERR_CONNECTION_RESET",
  ]);
});

test("diagnostics accumulates events across replacement browser contexts", () => {
  const firstPage = new EventEmitter();
  const secondPage = new EventEmitter();
  const diagnostics = attachPageDiagnostics(firstPage as never);

  const reused = (attachPageDiagnostics as unknown as Function)(secondPage, diagnostics);
  firstPage.emit("console", { type: () => "log", text: () => "before restart" });
  secondPage.emit("console", { type: () => "log", text: () => "after restart" });

  expect(reused).toBe(diagnostics);
  expect(diagnostics.consoleMessages).toEqual([
    "[log] before restart",
    "[log] after restart",
  ]);
});

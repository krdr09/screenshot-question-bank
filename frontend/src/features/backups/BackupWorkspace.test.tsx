import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BackupWorkspace } from "./BackupWorkspace";


const backup = {
  backup_id: "11111111-1111-4111-8111-111111111111",
  filename: "question-practice-backup-20260714-201500.zip",
  created_at: "2026-07-14T20:15:00+08:00",
  size: 4096,
  app_version: "0.1.0",
  schema_revision: "0007_practice",
  reason: "manual" as const,
  counts: { questions: 12, source_images: 4, crop_images: 12, attempts: 7, wrong_questions: 3 },
};

const validation = {
  validation_token: "v".repeat(48),
  backup_id: backup.backup_id,
  filename: backup.filename,
  created_at: backup.created_at,
  app_version: backup.app_version,
  schema_revision: backup.schema_revision,
  file_count: 31,
  uncompressed_bytes: 8192,
  counts: backup.counts,
  hashes_verified: true as const,
  migration_required: false,
  warnings: [] as string[],
};

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installHappyFetch() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url === "/api/backups" && (!init?.method || init.method === "GET")) return response({ items: [backup] });
    if (url === "/api/backups" && init?.method === "POST") return response(backup, 201);
    if (url === "/api/backups/validate") return response(validation);
    if (url === "/api/backups/restore") return response({
      restored_backup_id: backup.backup_id,
      safety_backup: { ...backup, filename: "question-practice-backup-20260714-202000-pre-restore.zip", reason: "pre_restore" },
      restart_required: true,
    });
    throw new Error(`unexpected request: ${url}`);
  });
}

afterEach(() => vi.restoreAllMocks());

describe("备份与恢复工作区", () => {
  it("lists, creates, refreshes and exposes a real download link", async () => {
    const fetchMock = installHappyFetch();
    const user = userEvent.setup();
    render(<BackupWorkspace />);

    expect(await screen.findByText(backup.filename)).toBeInTheDocument();
    expect(screen.getByLabelText("本机备份列表")).toHaveTextContent("12 道题目");
    expect(screen.getByRole("link", { name: `下载 ${backup.filename}` })).toHaveAttribute(
      "href",
      `/api/backups/${backup.backup_id}/content`,
    );

    await user.click(screen.getByRole("button", { name: "创建完整备份" }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1));
    expect(fetchMock.mock.calls.filter(([url, init]) => String(url) === "/api/backups" && (!init?.method || init.method === "GET"))).toHaveLength(2);
    expect(screen.getByText(/备份创建成功/)).toBeInTheDocument();
  });

  it("validates an existing backup and restores only after both confirmations", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const restarted = vi.fn();
    const fetchMock = installHappyFetch();
    const user = userEvent.setup();
    render(<BackupWorkspace onRestartRequired={restarted} />);

    await user.click(await screen.findByRole("radio", { name: new RegExp(backup.filename) }));
    await user.click(screen.getByRole("button", { name: "验证备份" }));
    expect(await screen.findByText("文件哈希全部通过")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "验证摘要" }).parentElement).toHaveTextContent("31 个文件");
    expect(screen.getByRole("button", { name: "确认恢复" })).toBeDisabled();

    await user.click(screen.getByRole("checkbox", { name: /已了解恢复会替换当前题库/ }));
    await user.click(screen.getByRole("button", { name: "确认恢复" }));

    expect(confirm).toHaveBeenCalledOnce();
    const restoreCall = fetchMock.mock.calls.find(([url]) => String(url) === "/api/backups/restore");
    expect(restoreCall?.[1]).toMatchObject({ method: "POST" });
    expect(JSON.parse(String(restoreCall?.[1]?.body))).toEqual({ validation_token: validation.validation_token, confirmed: true });
    expect(await screen.findByText(/请重启应用/)).toBeInTheDocument();
    expect(screen.getByText(/20260714-202000-pre-restore/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: `下载 ${backup.filename}` })).not.toBeInTheDocument();
    expect(screen.getByText("下载")).toHaveAttribute("aria-disabled", "true");
    expect(restarted).toHaveBeenCalledOnce();
  });

  it("uses external ZIP FormData and keeps existing and file sources mutually exclusive", async () => {
    const fetchMock = installHappyFetch();
    const user = userEvent.setup();
    render(<BackupWorkspace />);
    const existing = await screen.findByRole("radio", { name: new RegExp(backup.filename) });
    await user.click(existing);

    const file = new File(["zip"], "external.zip", { type: "application/zip" });
    await user.upload(screen.getByLabelText("选择外部 ZIP 备份"), file);
    expect(existing).not.toBeChecked();
    expect(screen.getByText("已选择 external.zip")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "验证备份" }));

    const call = fetchMock.mock.calls.find(([url]) => String(url) === "/api/backups/validate");
    expect(call?.[1]?.body).toBeInstanceOf(FormData);
    expect((call?.[1]?.body as FormData).get("file")).toBe(file);
    expect((call?.[1]?.body as FormData).has("backup_id")).toBe(false);

    await user.click(existing);
    expect(screen.queryByText("已选择 external.zip")).not.toBeInTheDocument();
  });

  it("shows migration warnings from a verified older backup", async () => {
    installHappyFetch().mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/backups" && (!init?.method || init.method === "GET")) return response({ items: [backup] });
      if (url === "/api/backups/validate") return response({ ...validation, migration_required: true, warnings: ["恢复后启动时将迁移数据库"] });
      throw new Error(`unexpected request: ${url}`);
    });
    const user = userEvent.setup();
    render(<BackupWorkspace />);
    await user.click(await screen.findByRole("radio", { name: new RegExp(backup.filename) }));
    await user.click(screen.getByRole("button", { name: "验证备份" }));

    expect(await screen.findByText("恢复后需要数据库迁移")).toBeInTheDocument();
    expect(screen.getByText("恢复后启动时将迁移数据库")).toBeInTheDocument();
  });

  it("reports validation failure without fabricating a restore confirmation", async () => {
    installHappyFetch().mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/backups" && (!init?.method || init.method === "GET")) return response({ items: [backup] });
      if (url === "/api/backups/validate") return response({ detail: "备份文件已损坏或被修改" }, 400);
      throw new Error(`unexpected request: ${url}`);
    });
    const user = userEvent.setup();
    render(<BackupWorkspace />);
    await user.click(await screen.findByRole("radio", { name: new RegExp(backup.filename) }));
    await user.click(screen.getByRole("button", { name: "验证备份" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("备份文件已损坏或被修改");
    expect(screen.queryByRole("button", { name: "确认恢复" })).not.toBeInTheDocument();
  });

  it("cancels the second confirmation without a restore request", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const fetchMock = installHappyFetch();
    const user = userEvent.setup();
    render(<BackupWorkspace />);
    await user.click(await screen.findByRole("radio", { name: new RegExp(backup.filename) }));
    await user.click(screen.getByRole("button", { name: "验证备份" }));
    await user.click(await screen.findByRole("checkbox", { name: /已了解恢复会替换当前题库/ }));
    await user.click(screen.getByRole("button", { name: "确认恢复" }));

    expect(confirm).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls.some(([url]) => String(url) === "/api/backups/restore")).toBe(false);
  });

  it("disables actions while creating and retains validation summary after restore error", async () => {
    let finishCreate!: (value: Response) => void;
    const pendingCreate = new Promise<Response>((resolve) => { finishCreate = resolve; });
    const fetchMock = installHappyFetch().mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/backups" && (!init?.method || init.method === "GET")) return response({ items: [backup] });
      if (url === "/api/backups" && init?.method === "POST") return pendingCreate;
      if (url === "/api/backups/validate") return response(validation);
      if (url === "/api/backups/restore") return response({ detail: "恢复失败，当前数据未被替换" }, 500);
      throw new Error(`unexpected request: ${url}`);
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<BackupWorkspace />);
    await screen.findByText(backup.filename);

    await user.click(screen.getByRole("button", { name: "创建完整备份" }));
    expect(screen.getByRole("button", { name: "正在创建备份…" })).toBeDisabled();
    finishCreate(response(backup, 201));
    await screen.findByText(/备份创建成功/);

    await user.click(screen.getByRole("radio", { name: new RegExp(backup.filename) }));
    await user.click(screen.getByRole("button", { name: "验证备份" }));
    await user.click(await screen.findByRole("checkbox", { name: /已了解恢复会替换当前题库/ }));
    await user.click(screen.getByRole("button", { name: "确认恢复" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("恢复失败，当前数据未被替换");
    expect(screen.getByText("文件哈希全部通过")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url]) => String(url) === "/api/backups/restore")).toBe(true);
  });
});

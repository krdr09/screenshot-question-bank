export const acceptedExtensions = new Set(["png", "jpg", "jpeg", "webp"]);

export type ImportFile = {
  file: File;
  relativePath: string;
};

export type ImportMapping = {
  key: string;
  courseName: string;
  categoryPath: string;
  batchName: string;
  sourceRelativePath: string;
  files: ImportFile[];
};

export type MappingResult = {
  mappings: ImportMapping[];
  ignoredCount: number;
};

function relativePath(file: File): string {
  return (file.webkitRelativePath || file.name).replaceAll("\\", "/");
}

export function mapSelectedFiles(selected: File[]): MappingResult {
  const grouped = new Map<string, ImportMapping>();
  let ignoredCount = 0;

  for (const file of selected) {
    const path = relativePath(file);
    const extension = file.name.split(".").pop()?.toLowerCase() ?? "";
    const parts = path.split("/").filter(Boolean);
    if (
      !acceptedExtensions.has(extension) ||
      parts.includes("..") ||
      path.startsWith("/")
    ) {
      ignoredCount += 1;
      continue;
    }

    const hasDirectorySelection = Boolean(file.webkitRelativePath);
    const withoutRoot = hasDirectorySelection ? parts.slice(1) : parts;
    const directories = withoutRoot.slice(0, -1);
    let courseName = "";
    let categoryPath = "";
    let batchName = "";
    let sourceRelativePath = "";

    if (directories.length >= 2) {
      courseName = directories[0];
      batchName = directories.at(-1) ?? "";
      categoryPath = directories.slice(1, -1).join("/");
      sourceRelativePath = directories.join("/");
    } else if (directories.length === 1) {
      courseName = directories[0];
      sourceRelativePath = directories[0];
    }

    const key = sourceRelativePath || "__flat__";
    const mapping = grouped.get(key) ?? {
      key,
      courseName,
      categoryPath,
      batchName,
      sourceRelativePath,
      files: [],
    };
    mapping.files.push({ file, relativePath: path });
    grouped.set(key, mapping);
  }

  return { mappings: [...grouped.values()], ignoredCount };
}


export interface RedactionContext {
  userHome?: string;
  workspacePath?: string;
  tempPath?: string;
  privateFilenames?: string[];
  syntheticFilenames?: string[];
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function replaceLiteralInsensitive(text: string, value: string, replacement: string): string {
  if (!value) return text;
  return text.replace(new RegExp(escapeRegExp(value), "gi"), replacement);
}

export function redactText(text: string, context: RedactionContext): string {
  let redacted = text;
  for (const filename of context.privateFilenames ?? []) {
    if (!(context.syntheticFilenames ?? []).includes(filename)) {
      redacted = replaceLiteralInsensitive(redacted, filename, "<private-file>");
    }
  }
  const paths = [
    [context.workspacePath, "<workspace>"],
    [context.tempPath, "<temp>"],
    [context.userHome, "<user-home>"],
  ] as const;
  for (const [sensitivePath, replacement] of [...paths].sort(
    (left, right) => (right[0]?.length ?? 0) - (left[0]?.length ?? 0),
  )) {
    if (sensitivePath) {
      redacted = replaceLiteralInsensitive(redacted, sensitivePath, replacement);
      redacted = replaceLiteralInsensitive(
        redacted,
        sensitivePath.replaceAll("\\", "/"),
        replacement,
      );
    }
  }
  redacted = redacted.replace(/[A-Za-z]:[\\/]Users[\\/][^\\/\s"'<>]+/gi, "<user-home>");
  redacted = redacted.replace(
    /(["'](?:authorization|(?:set-)?cookie)["']\s*:\s*)(["'])[^"'\r\n]*\2/gi,
    "$1$2<redacted>$2",
  );
  redacted = redacted.replace(
    /\b(authorization\s*:\s*)(?:bearer\s+)?[^\r\n<]+/gi,
    "$1<redacted>",
  );
  redacted = redacted.replace(
    /\b((?:set-)?cookie\s*:\s*)[^\r\n<]+/gi,
    "$1<redacted>",
  );
  redacted = redacted.replace(
    /\b(access[_-]?token|api[_-]?key|secret|password)\s*([:=])\s*[^\s&;,"'<>]+/gi,
    "$1$2<redacted>",
  );
  return redacted;
}

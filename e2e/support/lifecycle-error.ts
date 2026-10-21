export type CleanupResource = "server" | "browser" | "scenario";

export class ResourceCleanupError extends Error {
  readonly resource: CleanupResource;
  readonly cleanupVerified: boolean;
  readonly remainingPids: number[];

  constructor(
    resource: CleanupResource,
    cleanupVerified: boolean,
    remainingPids: number[],
    message: string,
    cause?: unknown,
  ) {
    super(message, cause === undefined ? undefined : { cause });
    this.name = "ResourceCleanupError";
    this.resource = resource;
    this.cleanupVerified = cleanupVerified;
    this.remainingPids = [...new Set(remainingPids)];
  }
}

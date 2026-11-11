import { realpathSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const canonicalize = typeof realpathSync.native === "function"
  ? realpathSync.native
  : realpathSync;

function normalizedPath(value, platform = process.platform) {
  const normalized = path.normalize(value);
  return platform === "win32" ? normalized.toLowerCase() : normalized;
}

/**
 * Return true only when argvPath and importMetaUrl resolve to the same existing
 * filesystem entry. Resolving both sides makes the check safe when a CLI is
 * reached through a symlinked deployment root. Any missing or malformed path
 * fails closed and is treated as an import instead of a direct invocation.
 */
export function isMainEntry(importMetaUrl, argvPath = process.argv[1], {
  platform = process.platform,
  realpath = canonicalize
} = {}) {
  if (
    typeof importMetaUrl !== "string" ||
    typeof argvPath !== "string" ||
    argvPath.length === 0 ||
    argvPath.includes("\0")
  ) {
    return false;
  }

  try {
    const modulePath = fileURLToPath(importMetaUrl);
    const canonicalModule = realpath(modulePath);
    const canonicalArgv = realpath(path.resolve(argvPath));
    return normalizedPath(canonicalModule, platform) === normalizedPath(canonicalArgv, platform);
  } catch {
    return false;
  }
}

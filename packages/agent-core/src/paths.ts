import * as path from "node:path";

/** Same rules as the backend's normalize_path, so both sides agree on every path. */
export class UnsafePathError extends Error {
  override name = "UnsafePathError";
}

const MAX_PATH_LENGTH = 400;
const DRIVE = /^[A-Za-z]:/;
const FORBIDDEN = new Set(["..", ".git"]);

export function normalizePath(raw: string): string {
  if (typeof raw !== "string") throw new UnsafePathError("Path must be a string");
  const p = raw.trim().replace(/\\/g, "/");
  if (!p) throw new UnsafePathError("Path is empty");
  if (p.includes("\0")) throw new UnsafePathError(`Path contains a NUL byte: ${raw}`);
  if (p.startsWith("/") || DRIVE.test(p)) throw new UnsafePathError(`Path must be relative: ${raw}`);
  const parts = p.split("/").filter((s) => s !== "" && s !== ".");
  if (parts.length === 0) throw new UnsafePathError(`Path is empty: ${raw}`);
  for (const part of parts) {
    if (FORBIDDEN.has(part)) throw new UnsafePathError(`Path segment '${part}' is not allowed: ${raw}`);
  }
  const clean = parts.join("/");
  if (clean.length > MAX_PATH_LENGTH) throw new UnsafePathError(`Path is too long: ${raw.slice(0, 60)}...`);
  return clean;
}

/** Resolve a normalized relative path inside root, refusing anything that escapes it. */
export function resolveInside(root: string, relative: string): string {
  const clean = normalizePath(relative);
  const base = path.resolve(root);
  const target = path.resolve(base, ...clean.split("/"));
  const rel = path.relative(base, target);
  if (rel === "" || rel.startsWith("..") || path.isAbsolute(rel)) {
    throw new UnsafePathError(`Path escapes the workspace: ${relative}`);
  }
  return target;
}

/** Convert an OS path under root to the forward-slash form used everywhere else. */
export function toRelative(root: string, absolute: string): string {
  return path.relative(path.resolve(root), absolute).split(path.sep).join("/");
}

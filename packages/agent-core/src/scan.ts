import { promises as fs } from "node:fs";
import * as path from "node:path";
import ignore, { type Ignore } from "ignore";
import { toRelative } from "./paths";

export interface ScanOptions {
  /** Skip files larger than this (bytes). */
  maxFileBytes: number;
  /** Stop when the total would exceed this (bytes). */
  maxTotalBytes: number;
  maxFiles: number;
  /** Extra gitignore-style patterns from settings. */
  exclude?: string[];
}

export interface ScannedFile {
  path: string;
  content: string;
  bytes: number;
}

export interface SkippedFile {
  path: string;
  reason: "ignored" | "binary" | "too-large" | "limit" | "unreadable";
}

export interface ScanResult {
  files: ScannedFile[];
  skipped: SkippedFile[];
  totalBytes: number;
  truncated: boolean;
}

export const DEFAULT_SCAN: ScanOptions = {
  maxFileBytes: 500_000,
  maxTotalBytes: 5_000_000,
  maxFiles: 2000,
};

/** Directories and files that never belong in a task upload. */
export const ALWAYS_EXCLUDED = [
  ".git/",
  "node_modules/",
  ".venv/",
  "venv/",
  "env/",
  "__pycache__/",
  ".mypy_cache/",
  ".pytest_cache/",
  ".ruff_cache/",
  ".tox/",
  "dist/",
  "build/",
  "out/",
  ".next/",
  ".nuxt/",
  "target/",
  "bin/",
  "obj/",
  "coverage/",
  "htmlcov/",
  ".idea/",
  ".vs/",
  ".turbo/",
  ".gradle/",
  ".DS_Store",
  "*.pyc",
  "*.vsix",
  "*.zip",
  "*.log",
  // secrets must never leave the machine
  ".env",
  ".env.*",
  "!.env.example",
  "*.pem",
  "*.key",
  "*.pfx",
  "*.p12",
  "id_rsa*",
  ".aws/",
  ".ssh/",
];

export function looksBinary(buffer: Buffer): boolean {
  const sample = buffer.subarray(0, 8000);
  return sample.includes(0);
}

async function loadGitignore(root: string): Promise<string[]> {
  try {
    const text = await fs.readFile(path.join(root, ".gitignore"), "utf8");
    return text.split(/\r?\n/);
  } catch {
    return [];
  }
}

/**
 * Walk the workspace in a stable (sorted) order and return text files to upload.
 * Honours the root .gitignore, built-in excludes (including secrets) and size limits.
 */
export async function scanWorkspace(root: string, options: Partial<ScanOptions> = {}): Promise<ScanResult> {
  const opts = { ...DEFAULT_SCAN, ...options };
  const ig: Ignore = ignore()
    .add(ALWAYS_EXCLUDED)
    .add(await loadGitignore(root))
    .add(opts.exclude ?? []);
  const files: ScannedFile[] = [];
  const skipped: SkippedFile[] = [];
  let totalBytes = 0;
  let truncated = false;

  async function walk(dir: string): Promise<void> {
    let entries;
    try {
      entries = await fs.readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    entries.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    for (const entry of entries) {
      if (truncated) return;
      const absolute = path.join(dir, entry.name);
      const rel = toRelative(root, absolute);
      if (entry.isSymbolicLink()) continue; // never follow links out of the workspace
      if (entry.isDirectory()) {
        if (ig.ignores(`${rel}/`)) continue;
        await walk(absolute);
        continue;
      }
      if (!entry.isFile()) continue;
      if (ig.ignores(rel)) {
        skipped.push({ path: rel, reason: "ignored" });
        continue;
      }
      let stat;
      try {
        stat = await fs.stat(absolute);
      } catch {
        skipped.push({ path: rel, reason: "unreadable" });
        continue;
      }
      if (stat.size > opts.maxFileBytes) {
        skipped.push({ path: rel, reason: "too-large" });
        continue;
      }
      if (files.length >= opts.maxFiles || totalBytes + stat.size > opts.maxTotalBytes) {
        skipped.push({ path: rel, reason: "limit" });
        truncated = true;
        return;
      }
      let buffer: Buffer;
      try {
        buffer = await fs.readFile(absolute);
      } catch {
        skipped.push({ path: rel, reason: "unreadable" });
        continue;
      }
      if (looksBinary(buffer)) {
        skipped.push({ path: rel, reason: "binary" });
        continue;
      }
      const content = buffer.toString("utf8");
      const bytes = Buffer.byteLength(content, "utf8");
      files.push({ path: rel, content, bytes });
      totalBytes += bytes;
    }
  }

  await walk(path.resolve(root));
  return { files, skipped, totalBytes, truncated };
}

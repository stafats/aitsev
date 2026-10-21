import { createHash } from "node:crypto";
import { promises as fs } from "node:fs";
import * as path from "node:path";
import type { FileChange } from "./client";
import { normalizePath, resolveInside } from "./paths";

export function sha256(text: string): string {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

/** Hashes of the files as they were when the task was submitted. */
export type Snapshot = Record<string, string>;

export function snapshotOf(files: Record<string, string>): Snapshot {
  const snap: Snapshot = {};
  for (const [p, content] of Object.entries(files)) snap[p] = sha256(content);
  return snap;
}

export interface Conflict {
  path: string;
  reason: "changed-since-submit" | "created-since-submit" | "deleted-since-submit";
}

export interface ApplyReport {
  written: string[];
  deleted: string[];
  conflicts: Conflict[];
  applied: boolean;
}

async function readIfExists(file: string): Promise<string | null> {
  try {
    return await fs.readFile(file, "utf8");
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw err;
  }
}

/**
 * Find files the user changed locally after submitting the task, so applying the AI result
 * would overwrite their work.
 */
export async function findConflicts(root: string, changes: FileChange[], snapshot: Snapshot): Promise<Conflict[]> {
  const conflicts: Conflict[] = [];
  for (const change of changes) {
    const rel = normalizePath(change.path);
    const current = await readIfExists(resolveInside(root, rel));
    const before = snapshot[rel];
    if (before === undefined) {
      if (current !== null && change.action === "added") conflicts.push({ path: rel, reason: "created-since-submit" });
      continue;
    }
    if (current === null) conflicts.push({ path: rel, reason: "deleted-since-submit" });
    else if (sha256(current) !== before) conflicts.push({ path: rel, reason: "changed-since-submit" });
  }
  return conflicts;
}

/**
 * Write the approved result into the workspace. Refuses unsafe paths, and refuses to overwrite
 * local edits made after submission unless ``force`` is set.
 */
export async function applyChanges(
  root: string,
  changes: FileChange[],
  snapshot: Snapshot,
  options: { force?: boolean } = {},
): Promise<ApplyReport> {
  // validate everything before touching the disk
  const planned = changes.map((c) => ({ change: c, target: resolveInside(root, c.path), rel: normalizePath(c.path) }));
  for (const { change } of planned) {
    if (change.action !== "deleted" && typeof change.content !== "string") {
      throw new Error(`Change for ${change.path} has no content`);
    }
  }
  const conflicts = await findConflicts(root, changes, snapshot);
  if (conflicts.length > 0 && !options.force) {
    return { written: [], deleted: [], conflicts, applied: false };
  }
  const written: string[] = [];
  const deleted: string[] = [];
  for (const { change, target, rel } of planned) {
    if (change.action === "deleted") {
      await fs.rm(target, { force: true });
      deleted.push(rel);
    } else {
      await fs.mkdir(path.dirname(target), { recursive: true });
      const tmp = `${target}.aitsev-${process.pid}.tmp`;
      await fs.writeFile(tmp, change.content ?? "", "utf8");
      await fs.rename(tmp, target); // atomic replace on the same filesystem
      written.push(rel);
    }
  }
  return { written, deleted, conflicts, applied: true };
}

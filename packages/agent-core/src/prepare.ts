import type { TaskCreate, TaskSize } from "./client";
import { snapshotOf, type Snapshot } from "./apply";
import { detectVerifyPlan, type VerifyPlan } from "./detect";
import { normalizePath } from "./paths";
import { scanWorkspace, type ScanOptions, type ScanResult } from "./scan";
import { selectContext, type Selection } from "./select";

export interface PrepareOptions {
  title?: string;
  size?: TaskSize;
  /** Files the user explicitly chose (relative paths); always context when they fit. */
  pinned?: string[];
  scan?: Partial<ScanOptions>;
  maxContextBytes?: number;
  /** Overrides for the detected verification plan. Empty arrays mean "use detected". */
  setupCommands?: string[];
  verifyCommands?: string[];
  sandboxImage?: string | null;
}

export interface PreparedTask {
  body: TaskCreate;
  snapshot: Snapshot;
  scan: ScanResult;
  selection: Selection;
  verify: VerifyPlan;
  warnings: string[];
}

export function titleFrom(instructions: string): string {
  const firstLine = instructions.trim().split(/\r?\n/)[0] ?? "";
  const title = firstLine.length > 80 ? `${firstLine.slice(0, 77)}...` : firstLine;
  return title || "Untitled task";
}

/** Scan the workspace and build a ready-to-send task, plus a snapshot for safe apply later. */
export async function prepareTask(root: string, instructions: string, options: PrepareOptions = {}): Promise<PreparedTask> {
  if (!instructions.trim()) throw new Error("Describe the task first");
  const scan = await scanWorkspace(root, options.scan);
  const files: Record<string, string> = {};
  for (const f of scan.files) files[f.path] = f.content;

  const warnings: string[] = [];
  if (scan.truncated) {
    warnings.push(
      `The project is larger than the upload limit; ${scan.skipped.filter((s) => s.reason === "limit").length}+ files ` +
        "were left out, so sandbox verification may be incomplete.",
    );
  }
  const pinned = (options.pinned ?? []).map(normalizePath).filter((p) => p in files);
  const selection = selectContext(scan.files, instructions, {
    maxContextBytes: options.maxContextBytes ?? 400_000,
    pinned,
  });

  const detected = detectVerifyPlan(files);
  const verify: VerifyPlan = {
    ...detected,
    setupCommands: options.setupCommands?.length ? options.setupCommands : detected.setupCommands,
    verifyCommands: options.verifyCommands?.length ? options.verifyCommands : detected.verifyCommands,
    image: options.sandboxImage ?? detected.image,
  };
  warnings.push(...detected.notes);

  const body: TaskCreate = {
    title: options.title?.trim() || titleFrom(instructions),
    instructions: instructions.trim(),
    files,
    context_paths: selection.wholeProject ? [] : selection.contextPaths,
    setup_commands: verify.setupCommands,
    verify_commands: verify.verifyCommands,
    size: options.size ?? "auto",
    sandbox_image: verify.image,
  };
  return { body, snapshot: snapshotOf(files), scan, selection, verify, warnings };
}

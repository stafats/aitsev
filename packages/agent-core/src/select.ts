/**
 * Choose which files the models read (the "context"), within a byte budget.
 * Every scanned file is still uploaded for sandbox verification.
 *
 * Ranking, highest first:
 *  1. files the user pinned (selected in the editor or explorer)
 *  2. files whose path or name appears in the instructions
 *  3. files sharing words with the instructions (path words weigh more than content words)
 *  4. tests and sources paired with already-chosen files, and project manifests
 */
import type { ScannedFile } from "./scan";

export interface SelectOptions {
  maxContextBytes: number;
  pinned?: string[];
}

export interface Selection {
  contextPaths: string[];
  contextBytes: number;
  /** True when every file fits, so the context is the whole project. */
  wholeProject: boolean;
}

const MANIFESTS = new Set([
  "package.json",
  "pyproject.toml",
  "requirements.txt",
  "tsconfig.json",
  "setup.py",
  "setup.cfg",
  "go.mod",
  "Cargo.toml",
  "pom.xml",
  "build.gradle",
  "README.md",
]);

const STOP_WORDS = new Set(
  "the a an and or to of in on for with from by is are be this that it as at add make fix use new update change should when into all any can not".split(
    " ",
  ),
);

export function tokenize(text: string): string[] {
  return (
    text
      // split camelCase and snake_case before lowercasing
      .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
      .toLowerCase()
      .split(/[^a-z0-9]+/)
      .filter((w) => w.length > 2 && !STOP_WORDS.has(w))
  );
}

function baseName(p: string): string {
  return p.split("/").pop() ?? p;
}

function stem(p: string): string {
  return baseName(p)
    .replace(/\.[^.]+$/, "")
    .replace(/^test_|_test$|\.test$|\.spec$/g, "")
    .toLowerCase();
}

export function isTestFile(p: string): boolean {
  const name = baseName(p).toLowerCase();
  return (
    /^test_.*\.py$/.test(name) ||
    /_test\.(py|go)$/.test(name) ||
    /\.(test|spec)\.[jt]sx?$/.test(name) ||
    p.toLowerCase().split("/").includes("tests") ||
    p.toLowerCase().split("/").includes("__tests__")
  );
}

export function selectContext(files: ScannedFile[], instructions: string, options: SelectOptions): Selection {
  const total = files.reduce((n, f) => n + f.bytes, 0);
  if (total <= options.maxContextBytes) {
    return { contextPaths: files.map((f) => f.path).sort(), contextBytes: total, wholeProject: true };
  }

  const pinned = new Set(options.pinned ?? []);
  const lowered = instructions.toLowerCase();
  const words = new Set(tokenize(instructions));
  const score = new Map<string, number>();

  for (const f of files) {
    let s = 0;
    if (pinned.has(f.path)) s += 10_000;
    if (lowered.includes(f.path.toLowerCase())) s += 1_000;
    else if (lowered.includes(baseName(f.path).toLowerCase())) s += 500;
    for (const w of tokenize(f.path)) if (words.has(w)) s += 40;
    if (words.size > 0) {
      const contentWords = tokenize(f.content.slice(0, 50_000));
      let hits = 0;
      for (const w of contentWords) if (words.has(w)) hits += 1;
      // diminishing returns, normalised a little by size so huge files do not win on volume
      s += Math.min(60, (hits * 400) / Math.max(400, contentWords.length));
    }
    if (MANIFESTS.has(baseName(f.path)) && !f.path.includes("/")) s += 25;
    score.set(f.path, s);
  }

  // pair tests and sources with strong candidates
  const byStem = new Map<string, ScannedFile[]>();
  for (const f of files) {
    const list = byStem.get(stem(f.path)) ?? [];
    list.push(f);
    byStem.set(stem(f.path), list);
  }
  for (const f of files) {
    const s = score.get(f.path) ?? 0;
    if (s < 100) continue;
    for (const partner of byStem.get(stem(f.path)) ?? []) {
      if (partner.path !== f.path && isTestFile(partner.path) !== isTestFile(f.path)) {
        score.set(partner.path, Math.max(score.get(partner.path) ?? 0, s / 2));
      }
    }
  }

  const ranked = [...files].sort(
    (a, b) => (score.get(b.path) ?? 0) - (score.get(a.path) ?? 0) || a.path.localeCompare(b.path),
  );
  const chosen: string[] = [];
  let used = 0;
  for (const f of ranked) {
    const s = score.get(f.path) ?? 0;
    if (s <= 0 && chosen.length > 0) break;
    if (used + f.bytes > options.maxContextBytes) {
      if (pinned.has(f.path)) continue; // pinned but too big for the remaining budget
      continue;
    }
    chosen.push(f.path);
    used += f.bytes;
  }
  return { contextPaths: chosen.sort(), contextBytes: used, wholeProject: false };
}

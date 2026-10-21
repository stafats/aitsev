import { execFile } from "node:child_process";
import { promisify } from "node:util";

const run = promisify(execFile);

async function git(cwd: string, args: string[]): Promise<string> {
  const { stdout } = await run("git", args, { cwd, maxBuffer: 10 * 1024 * 1024, windowsHide: true });
  return stdout;
}

export async function isGitRepo(cwd: string): Promise<boolean> {
  try {
    return (await git(cwd, ["rev-parse", "--is-inside-work-tree"])).trim() === "true";
  } catch {
    return false;
  }
}

export async function currentBranch(cwd: string): Promise<string | null> {
  try {
    const name = (await git(cwd, ["rev-parse", "--abbrev-ref", "HEAD"])).trim();
    return name && name !== "HEAD" ? name : null;
  } catch {
    return null;
  }
}

/** Create and switch to a new branch for the AI change. */
export async function createBranch(cwd: string, name: string): Promise<void> {
  if (!/^[A-Za-z0-9._/-]{1,100}$/.test(name) || name.includes("..")) {
    throw new Error(`Invalid branch name: ${name}`);
  }
  await git(cwd, ["switch", "-c", name]);
}

/**
 * Stage exactly the given paths (including deletions) and commit them. Other local changes are
 * left untouched. Returns the new commit hash.
 */
export async function commitPaths(cwd: string, paths: string[], message: string): Promise<string> {
  if (paths.length === 0) throw new Error("Nothing to commit");
  await git(cwd, ["add", "--all", "--", ...paths]);
  await git(cwd, ["commit", "--only", "-m", message, "--", ...paths]);
  return (await git(cwd, ["rev-parse", "HEAD"])).trim();
}

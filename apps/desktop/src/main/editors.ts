/**
 * Finds VS Code-family editors on this computer and installs the Aitsev extension bundled with
 * the desktop app into them, using each editor's own command-line tool.
 */
import { spawn } from "node:child_process";
import { promises as fs } from "node:fs";
import * as path from "node:path";

export interface EditorDef {
  id: string;
  name: string;
  cli: string;
  windows: string[]; // relative to %LOCALAPPDATA% or %ProgramFiles%
  mac: string; // .app bundle name
}

export const EDITORS: EditorDef[] = [
  { id: "vscode", name: "Visual Studio Code", cli: "code", windows: ["Programs\\Microsoft VS Code\\bin\\code.cmd", "Microsoft VS Code\\bin\\code.cmd"], mac: "Visual Studio Code.app" },
  { id: "cursor", name: "Cursor", cli: "cursor", windows: ["Programs\\cursor\\resources\\app\\bin\\cursor.cmd", "Programs\\Cursor\\resources\\app\\bin\\cursor.cmd"], mac: "Cursor.app" },
  { id: "kiro", name: "Kiro", cli: "kiro", windows: ["Programs\\Kiro\\bin\\kiro.cmd", "Kiro\\bin\\kiro.cmd"], mac: "Kiro.app" },
  { id: "windsurf", name: "Windsurf", cli: "windsurf", windows: ["Programs\\Windsurf\\bin\\windsurf.cmd", "Windsurf\\bin\\windsurf.cmd"], mac: "Windsurf.app" },
  { id: "vscodium", name: "VSCodium", cli: "codium", windows: ["Programs\\VSCodium\\bin\\codium.cmd", "VSCodium\\bin\\codium.cmd"], mac: "VSCodium.app" },
];

export interface FoundEditor {
  id: string;
  name: string;
  cliPath: string | null;
}

export type Exists = (file: string) => Promise<boolean>;

export const fileExists: Exists = async (file) => {
  try {
    await fs.access(file);
    return true;
  } catch {
    return false;
  }
};

export function candidatePaths(editor: EditorDef, platform: NodeJS.Platform, env: NodeJS.ProcessEnv): string[] {
  const join = platform === "win32" ? path.win32.join : path.posix.join;
  const out: string[] = [];
  if (platform === "win32") {
    for (const base of [env["LOCALAPPDATA"], env["ProgramFiles"], env["ProgramFiles(x86)"]]) {
      if (base) for (const rel of editor.windows) out.push(join(base, rel));
    }
  } else if (platform === "darwin") {
    const bundle = `${editor.mac}/Contents/Resources/app/bin/${editor.cli}`;
    out.push(join("/Applications", bundle));
    if (env["HOME"]) out.push(join(env["HOME"], "Applications", bundle));
  }
  const pathVar = env["PATH"] ?? env["Path"] ?? "";
  const names = platform === "win32" ? [`${editor.cli}.cmd`, `${editor.cli}.exe`] : [editor.cli];
  for (const dir of pathVar.split(platform === "win32" ? ";" : ":")) {
    if (dir) for (const name of names) out.push(join(dir, name));
  }
  return out;
}

export async function detectEditors(platform: NodeJS.Platform, env: NodeJS.ProcessEnv, exists: Exists = fileExists): Promise<FoundEditor[]> {
  const found: FoundEditor[] = [];
  for (const editor of EDITORS) {
    let cliPath: string | null = null;
    for (const candidate of candidatePaths(editor, platform, env)) {
      if (await exists(candidate)) {
        cliPath = candidate;
        break;
      }
    }
    found.push({ id: editor.id, name: editor.name, cliPath });
  }
  return found;
}

export type Runner = (command: string, args: string[], windows: boolean) => Promise<{ code: number; output: string }>;

export const runCli: Runner = (command, args, windows) =>
  new Promise((resolve) => {
    // Editor CLIs on Windows are .cmd scripts, which must run through the shell; every argument
    // is a path we control, quoted (Windows paths cannot contain double quotes).
    const child = windows
      ? spawn(`"${command}"`, args.map((a) => (a.startsWith("--") ? a : `"${a}"`)), { shell: true, windowsHide: true })
      : spawn(command, args, { windowsHide: true });
    let output = "";
    const timer = setTimeout(() => child.kill(), 120_000);
    child.stdout?.on("data", (d: Buffer) => (output += d.toString()));
    child.stderr?.on("data", (d: Buffer) => (output += d.toString()));
    child.on("error", (err) => {
      clearTimeout(timer);
      resolve({ code: -1, output: err.message });
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code: code ?? -1, output });
    });
  });

export interface InstallResult {
  id: string;
  name: string;
  ok: boolean;
  error?: string;
}

export async function installExtension(editors: FoundEditor[], ids: string[], vsixPath: string, platform: NodeJS.Platform, run: Runner = runCli): Promise<InstallResult[]> {
  const results: InstallResult[] = [];
  for (const editor of editors.filter((e) => ids.includes(e.id))) {
    if (!editor.cliPath) {
      results.push({ id: editor.id, name: editor.name, ok: false, error: "Not installed on this computer" });
      continue;
    }
    const { code, output } = await run(editor.cliPath, ["--install-extension", vsixPath, "--force"], platform === "win32");
    results.push(code === 0
      ? { id: editor.id, name: editor.name, ok: true }
      : { id: editor.id, name: editor.name, ok: false, error: output.trim().split("\n").slice(-1)[0] || `Exited with ${code}` });
  }
  return results;
}

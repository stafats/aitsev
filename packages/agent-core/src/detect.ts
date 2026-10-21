/**
 * Guess how to verify a project in the sandbox. The user can always override these in settings.
 *
 * Setup commands run with network access and must install into the workspace (the sandbox
 * HOME is /workspace/.home), because verify commands run without network.
 */
export interface VerifyPlan {
  kind: "python" | "node" | "unknown";
  image: string | null;
  setupCommands: string[];
  verifyCommands: string[];
  notes: string[];
}

interface PackageJson {
  scripts?: Record<string, string>;
  packageManager?: string;
}

function parsePackageJson(text: string | undefined): PackageJson | null {
  if (!text) return null;
  try {
    const value: unknown = JSON.parse(text);
    return value && typeof value === "object" ? (value as PackageJson) : null;
  } catch {
    return null;
  }
}

const NPM_DEFAULT_TEST = /no test specified/i;

export function detectVerifyPlan(files: Map<string, string> | Record<string, string>): VerifyPlan {
  const get = (p: string): string | undefined =>
    files instanceof Map ? files.get(p) : (files as Record<string, string>)[p];
  const has = (p: string): boolean => get(p) !== undefined;
  const paths = files instanceof Map ? [...files.keys()] : Object.keys(files);

  const pkg = parsePackageJson(get("package.json"));
  if (pkg) {
    const scripts = pkg.scripts ?? {};
    const pnpm = has("pnpm-lock.yaml") || (pkg.packageManager ?? "").startsWith("pnpm");
    const notes: string[] = [];
    let setup: string;
    let run: (script: string) => string;
    if (pnpm) {
      setup =
        "npm install --no-save --no-audit --no-fund --prefix .aitsev-tools pnpm@9 && " +
        (has("pnpm-lock.yaml")
          ? ".aitsev-tools/node_modules/.bin/pnpm install --frozen-lockfile"
          : ".aitsev-tools/node_modules/.bin/pnpm install");
      run = (s) => `.aitsev-tools/node_modules/.bin/pnpm run ${s}`;
    } else {
      setup = has("package-lock.json") ? "npm ci --no-audit --no-fund" : "npm install --no-audit --no-fund";
      run = (s) => `npm run ${s} --silent`;
      if (has("yarn.lock")) notes.push("yarn.lock found; using npm to install. Override in settings if needed.");
    }
    const verify: string[] = [];
    for (const name of ["typecheck", "lint", "build"]) if (scripts[name]) verify.push(run(name));
    if (scripts.test && !NPM_DEFAULT_TEST.test(scripts.test)) verify.push(run("test"));
    if (verify.length === 0) notes.push("No test, build, lint or typecheck script found in package.json.");
    return { kind: "node", image: "node:22-slim", setupCommands: [setup], verifyCommands: verify, notes };
  }

  const pyproject = get("pyproject.toml");
  const python =
    pyproject !== undefined ||
    has("requirements.txt") ||
    has("setup.py") ||
    paths.some((p) => p.endsWith(".py"));
  if (python) {
    const hasTests =
      paths.some((p) => /(^|\/)(test_[^/]*|[^/]*_test)\.py$/.test(p)) ||
      (pyproject ?? "").includes("[tool.pytest");
    const notes: string[] = [];
    const setup: string[] = [];
    const verify: string[] = [];
    if (has("uv.lock")) {
      setup.push("pip install --quiet --user uv && ~/.local/bin/uv sync --frozen");
      if (hasTests) verify.push("~/.local/bin/uv run --frozen --offline pytest -q");
    } else {
      const installs = ["python -m venv .venv", ".venv/bin/pip install --quiet --upgrade pip"];
      if (has("requirements.txt")) installs.push(".venv/bin/pip install --quiet -r requirements.txt");
      if (pyproject !== undefined || has("setup.py")) installs.push(".venv/bin/pip install --quiet -e .");
      if (hasTests) installs.push(".venv/bin/pip install --quiet pytest");
      setup.push(installs.join(" && "));
      if (hasTests) verify.push(".venv/bin/python -m pytest -q");
    }
    if (!hasTests) {
      verify.push(`${has("uv.lock") ? "python" : ".venv/bin/python"} -m compileall -q -x "/\\.(venv|home)/" .`);
      notes.push("No tests found; verification only checks that every file compiles.");
    }
    return { kind: "python", image: "python:3.12-slim", setupCommands: setup, verifyCommands: verify, notes };
  }

  return {
    kind: "unknown",
    image: null,
    setupCommands: [],
    verifyCommands: [],
    notes: ["Could not detect the project type; set aitsev.verifyCommands to enable sandbox checks."],
  };
}

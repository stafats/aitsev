// Bundles the extension and @aitsev/agent-core into one CommonJS file for VS Code.
import { build } from "esbuild";

const production = process.argv.includes("--production");

await build({
  entryPoints: ["src/extension.ts"],
  bundle: true,
  outfile: "dist/extension.js",
  platform: "node",
  format: "cjs",
  target: "node20",
  external: ["vscode"],
  sourcemap: !production,
  minify: production,
  // resolve the workspace package from its TypeScript source, so no separate build is needed
  alias: { "@aitsev/agent-core": "../../packages/agent-core/src/index.ts" },
  logLevel: "info",
});

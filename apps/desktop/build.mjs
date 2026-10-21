// Builds the desktop bundles into dist/ and, with --web, the browser app into dist-web/.
//   dist-web/landing.html, landing.css    -> served at /
//   dist-web/app/{index.html,app.js,styles.css} -> served at /app
import { build } from "esbuild";
import { copyFile, mkdir, readFile, writeFile } from "node:fs/promises";

const production = process.argv.includes("--production");
const web = process.argv.includes("--web");
const common = { bundle: true, sourcemap: !production, minify: production, logLevel: "info" };
// The server installed apps connect to. Release builds: the hosted Aitsev service.
const defaultServer = process.env.AITSEV_DEFAULT_SERVER ?? (production ? "https://app.aitsev.com" : "http://localhost:8000");

async function buildDesktop() {
  await mkdir("dist/renderer", { recursive: true });
  await Promise.all([
    build({ ...common, entryPoints: ["src/main/main.ts"], outfile: "dist/main.js", platform: "node", format: "cjs", target: "node20", external: ["electron"],
      define: { __AITSEV_DEFAULT_SERVER__: JSON.stringify(defaultServer) } }),
    build({ ...common, entryPoints: ["src/main/preload.ts"], outfile: "dist/preload.js", platform: "node", format: "cjs", target: "node20", external: ["electron"] }),
    build({ ...common, entryPoints: ["src/renderer/app.ts"], outfile: "dist/renderer/app.js", platform: "browser", format: "iife", target: "chrome120" }),
  ]);
  await copyFile("src/renderer/index.html", "dist/renderer/index.html");
  await copyFile("src/renderer/styles.css", "dist/renderer/styles.css");
}

async function buildWeb() {
  await mkdir("dist-web/app", { recursive: true });
  await build({
    ...common,
    sourcemap: false,
    minify: true,
    entryPoints: ["src/web/main.ts"],
    outfile: "dist-web/app/app.js",
    platform: "browser",
    format: "iife",
    target: ["chrome110", "firefox110", "safari16"],
  });
  const css = (await readFile("src/renderer/styles.css", "utf8")) + (await readFile("src/web/web.css", "utf8"));
  await writeFile("dist-web/app/styles.css", css);
  await copyFile("src/web/index.html", "dist-web/app/index.html");
  await copyFile("src/web/landing.html", "dist-web/landing.html");
  await copyFile("src/web/landing.css", "dist-web/landing.css");
}

if (web) await buildWeb();
else await buildDesktop();

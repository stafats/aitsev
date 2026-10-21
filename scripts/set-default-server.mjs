// Sets the server the editor extension connects to by default (release builds).
//   node scripts/set-default-server.mjs https://app.example.com
// The desktop app reads AITSEV_DEFAULT_SERVER at build time instead (apps/desktop/build.mjs).
import { readFile, writeFile } from "node:fs/promises";

const url = (process.argv[2] ?? "").replace(/\/+$/, "");
if (!/^https:\/\/[^/\s]+$/.test(url) && !/^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(url)) {
  console.error("Usage: node scripts/set-default-server.mjs https://your-server");
  process.exit(1);
}
const pkgPath = "apps/vscode/package.json";
const pkg = JSON.parse(await readFile(pkgPath, "utf8"));
pkg.contributes.configuration.properties["aitsev.serverUrl"].default = url;
await writeFile(pkgPath, `${JSON.stringify(pkg, null, 2)}\n`);

const ctrlPath = "apps/vscode/src/controller.ts";
const source = await readFile(ctrlPath, "utf8");
const updated = source.replace(/export const DEFAULT_SERVER = "[^"]*";/, `export const DEFAULT_SERVER = ${JSON.stringify(url)};`);
if (updated === source && !source.includes(`DEFAULT_SERVER = ${JSON.stringify(url)}`)) {
  console.error("DEFAULT_SERVER not found in controller.ts");
  process.exit(1);
}
await writeFile(ctrlPath, updated);
console.log(`Extension default server: ${url}`);

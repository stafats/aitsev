#!/usr/bin/env node
// Turns a Code-OSS checkout into the Aitsev IDE source: product identity, Open VSX gallery,
// icons and installer images. Run before `npm ci` / `npm run gulp ...` in the VS Code folder.
//
//   node ide/prepare.mjs --vscode ./vscode
import { copyFile, readFile, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const vscodeDir = path.resolve(args[args.indexOf("--vscode") + 1] ?? "vscode");
if (!existsSync(path.join(vscodeDir, "product.json"))) {
  console.error(`No product.json in ${vscodeDir}. Usage: node ide/prepare.mjs --vscode <vscode checkout>`);
  process.exit(1);
}

// 1. Product identity: merge the overlay into product.json.
const productPath = path.join(vscodeDir, "product.json");
const product = JSON.parse(await readFile(productPath, "utf8"));
const overlay = JSON.parse(await readFile(path.join(here, "product.overlay.json"), "utf8"));
const merged = { ...product, ...overlay };
// Keep Microsoft-hosted marketplace and telemetry settings out of the fork.
for (const key of ["enableTelemetry", "aiConfig", "msftInternalDomains", "tasConfig", "extensionsGallery_ms"]) delete merged[key];
await writeFile(productPath, `${JSON.stringify(merged, null, "\t")}\n`);

// 2. Icons, installer images and the empty-editor watermark.
const generated = path.join(here, "branding", "generated");
const copies = [
  ["code.ico", "resources/win32/code.ico"],
  ["code_150x150.png", "resources/win32/code_150x150.png"],
  ["code_70x70.png", "resources/win32/code_70x70.png"],
  ["code.icns", "resources/darwin/code.icns"],
  ["code.png", "resources/linux/code.png"],
  ...[100, 125, 150, 175, 200, 225, 250].flatMap((s) => [
    [`inno-big-${s}.bmp`, `resources/win32/inno-big-${s}.bmp`],
    [`inno-small-${s}.bmp`, `resources/win32/inno-small-${s}.bmp`],
  ]),
  ...["dark", "light", "hcDark", "hcLight"].map((t) => [`letterpress-${t}.svg`, `src/vs/workbench/browser/parts/editor/media/letterpress-${t}.svg`]),
];
for (const [from, to] of copies) {
  const target = path.join(vscodeDir, to);
  if (!existsSync(target)) {
    console.error(`Expected ${to} in the VS Code checkout; the VS Code layout changed.`);
    process.exit(1);
  }
  await copyFile(path.join(generated, from), target);
}

// 3. The installer's output name.
const issPath = path.join(vscodeDir, "build", "win32", "code.iss");
const iss = await readFile(issPath, "utf8");
if (!iss.includes("OutputBaseFilename=VSCodeSetup")) {
  console.error("build/win32/code.iss changed: cannot set the installer name.");
  process.exit(1);
}
await writeFile(issPath, iss.replace("OutputBaseFilename=VSCodeSetup", "OutputBaseFilename=Aitsev-Setup"));

console.log(`Aitsev branding applied to ${vscodeDir} (${product.nameShort} → ${merged.nameShort})`);

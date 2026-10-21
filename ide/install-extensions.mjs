#!/usr/bin/env node
// Puts the Aitsev extension (from its .vsix) and the IDE defaults into a built IDE as built-in
// extensions, so they are there on first launch and cannot be uninstalled by accident.
//
//   node ide/install-extensions.mjs --app ../VSCode-win32-x64 --vsix apps/vscode/aitsev.vsix
//   (macOS: --app ../VSCode-darwin-arm64/Aitsev.app)
import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import { inflateRawSync } from "node:zlib";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));

/** Minimal ZIP reader (stored and deflated entries), enough for a .vsix. */
export function readZip(buffer) {
  const entries = new Map();
  let eocd = -1;
  for (let i = buffer.length - 22; i >= Math.max(0, buffer.length - 65_557); i--) {
    if (buffer.readUInt32LE(i) === 0x06054b50) {
      eocd = i;
      break;
    }
  }
  if (eocd < 0) throw new Error("Not a zip file");
  const count = buffer.readUInt16LE(eocd + 10);
  let offset = buffer.readUInt32LE(eocd + 16);
  for (let n = 0; n < count; n++) {
    if (buffer.readUInt32LE(offset) !== 0x02014b50) throw new Error("Corrupt zip directory");
    const method = buffer.readUInt16LE(offset + 10);
    const compressed = buffer.readUInt32LE(offset + 20);
    const nameLength = buffer.readUInt16LE(offset + 28);
    const extraLength = buffer.readUInt16LE(offset + 30);
    const commentLength = buffer.readUInt16LE(offset + 32);
    const local = buffer.readUInt32LE(offset + 42);
    const name = buffer.toString("utf8", offset + 46, offset + 46 + nameLength);
    offset += 46 + nameLength + extraLength + commentLength;
    if (name.endsWith("/")) continue;
    const localName = buffer.readUInt16LE(local + 26);
    const localExtra = buffer.readUInt16LE(local + 28);
    const start = local + 30 + localName + localExtra;
    const data = buffer.subarray(start, start + compressed);
    if (method === 0) entries.set(name, Buffer.from(data));
    else if (method === 8) entries.set(name, inflateRawSync(data));
    else throw new Error(`Unsupported zip compression ${method} for ${name}`);
  }
  return entries;
}

export function extensionsDir(app) {
  return app.endsWith(".app") ? path.join(app, "Contents", "Resources", "app", "extensions") : path.join(app, "resources", "app", "extensions");
}

export async function installBuiltIns(app, vsix) {
  const target = extensionsDir(app);
  if (!existsSync(target)) throw new Error(`No built-in extensions folder at ${target}. Build the IDE first.`);
  const entries = readZip(await readFile(vsix));
  const manifest = entries.get("extension/package.json");
  if (!manifest) throw new Error(`${vsix} has no extension/package.json`);
  const pkg = JSON.parse(manifest.toString("utf8"));
  const dest = path.join(target, `${pkg.publisher}.${pkg.name}`);
  await rm(dest, { recursive: true, force: true });
  for (const [name, data] of entries) {
    if (!name.startsWith("extension/")) continue;
    const rel = name.slice("extension/".length);
    if (rel.split("/").includes("..")) throw new Error(`Unsafe path in vsix: ${name}`);
    const file = path.join(dest, rel);
    await mkdir(path.dirname(file), { recursive: true });
    await writeFile(file, data);
  }
  const defaults = path.join(target, "aitsev.aitsev-defaults");
  await rm(defaults, { recursive: true, force: true });
  await cp(path.join(here, "extensions", "aitsev-defaults"), defaults, { recursive: true });
  return [dest, defaults];
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  const get = (flag) => {
    const i = args.indexOf(flag);
    return i >= 0 ? args[i + 1] : undefined;
  };
  const app = get("--app");
  const vsix = get("--vsix");
  if (!app || !vsix) {
    console.error("Usage: node ide/install-extensions.mjs --app <built IDE folder or .app> --vsix <aitsev.vsix>");
    process.exit(1);
  }
  const installed = await installBuiltIns(path.resolve(app), path.resolve(vsix));
  console.log(`Built-in extensions installed:\n  ${installed.join("\n  ")}`);
}

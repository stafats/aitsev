// Writes the electron-builder config for a published release: the package.json "build" section
// plus GitHub Releases as the update feed (owner/repo from GITHUB_REPOSITORY).
import { readFile, writeFile } from "node:fs/promises";

const repo = process.env.GITHUB_REPOSITORY;
if (!repo || !repo.includes("/")) {
  console.error("Set GITHUB_REPOSITORY=owner/repo");
  process.exit(1);
}
const [owner, name] = repo.split("/");
const pkg = JSON.parse(await readFile("package.json", "utf8"));
const config = { ...pkg.build, publish: [{ provider: "github", owner, repo: name, releaseType: "release" }] };
if (process.env.AITSEV_SKIP_NOTARIZE === "1") config.mac = { ...config.mac, notarize: false };
await writeFile("electron-builder.release.json", JSON.stringify(config, null, 2));
console.log(`Release config written for ${owner}/${name}`);

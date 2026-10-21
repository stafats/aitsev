/**
 * Settings for the desktop app. The server URL is stored as JSON; the API key is encrypted with
 * the operating system's key store (Electron safeStorage) and never reaches the renderer.
 */
import { promises as fs } from "node:fs";
import * as path from "node:path";

export interface Encryptor {
  isEncryptionAvailable(): boolean;
  encryptString(text: string): Buffer;
  decryptString(data: Buffer): string;
}

export interface PublicSettings {
  serverUrl: string;
  hasApiKey: boolean;
  email?: string;
}

declare const __AITSEV_DEFAULT_SERVER__: string | undefined;

/** Baked in at build time (AITSEV_DEFAULT_SERVER); local server for development builds. */
export const DEFAULT_SERVER =
  typeof __AITSEV_DEFAULT_SERVER__ === "string" && __AITSEV_DEFAULT_SERVER__ ? __AITSEV_DEFAULT_SERVER__ : "http://localhost:8000";

export class SettingsError extends Error {
  override name = "SettingsError";
}

export function validateServerUrl(raw: string): string {
  let url: URL;
  try {
    url = new URL(raw.trim());
  } catch {
    throw new SettingsError("Enter a full server URL, e.g. https://aitsev.example.com");
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw new SettingsError("The server URL must start with http:// or https://");
  }
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (url.protocol === "http:" && !local) {
    throw new SettingsError("Use https:// for servers that are not on this computer");
  }
  if (url.username || url.password) throw new SettingsError("Do not put credentials in the URL");
  return url.toString().replace(/\/+$/, "");
}

export class SettingsStore {
  private readonly settingsFile: string;
  private readonly keyFile: string;

  constructor(
    private readonly dir: string,
    private readonly encryptor: Encryptor,
  ) {
    this.settingsFile = path.join(dir, "settings.json");
    this.keyFile = path.join(dir, "api-key.bin");
  }

  private async read(): Promise<{ serverUrl?: unknown; email?: unknown }> {
    try {
      return JSON.parse(await fs.readFile(this.settingsFile, "utf8")) as { serverUrl?: unknown; email?: unknown };
    } catch {
      return {};
    }
  }

  async serverUrl(): Promise<string> {
    const data = await this.read();
    return typeof data.serverUrl === "string" ? data.serverUrl : DEFAULT_SERVER;
  }

  async email(): Promise<string | undefined> {
    const data = await this.read();
    return typeof data.email === "string" && data.email ? data.email : undefined;
  }

  async apiKey(): Promise<string | null> {
    try {
      const data = await fs.readFile(this.keyFile);
      return this.encryptor.decryptString(data);
    } catch {
      return null;
    }
  }

  async publicSettings(): Promise<PublicSettings> {
    const hasApiKey = (await this.apiKey()) !== null;
    const email = hasApiKey ? await this.email() : undefined;
    return email ? { serverUrl: await this.serverUrl(), hasApiKey, email } : { serverUrl: await this.serverUrl(), hasApiKey };
  }

  async save(input: { serverUrl: string; apiKey?: string | null; email?: string }): Promise<PublicSettings> {
    const serverUrl = validateServerUrl(input.serverUrl);
    const previous = await this.read();
    const serverChanged = typeof previous.serverUrl === "string" && previous.serverUrl !== serverUrl;
    await fs.mkdir(this.dir, { recursive: true });
    if (typeof input.apiKey === "string" && input.apiKey.trim()) {
      const key = input.apiKey.trim();
      if (key.length < 16) throw new SettingsError("API keys are at least 16 characters");
      if (!this.encryptor.isEncryptionAvailable()) {
        throw new SettingsError("This system has no secure key store, so the API key cannot be saved safely");
      }
      await writeAtomic(this.keyFile, this.encryptor.encryptString(key));
    } else if (input.apiKey === null) {
      await fs.rm(this.keyFile, { force: true });
    }
    // The signed-in email belongs to the key: keep it only while that key stays.
    let email: string | undefined;
    if (typeof input.apiKey === "string") email = input.email;
    else if (input.apiKey === undefined && !serverChanged && typeof previous.email === "string") email = previous.email;
    const record: Record<string, string> = { serverUrl };
    if (email) record.email = email;
    await writeAtomic(this.settingsFile, Buffer.from(JSON.stringify(record, null, 2)));
    return this.publicSettings();
  }
}

async function writeAtomic(file: string, data: Buffer): Promise<void> {
  const tmp = `${file}.${process.pid}.tmp`;
  await fs.writeFile(tmp, data, { mode: 0o600 });
  await fs.rename(tmp, file);
}

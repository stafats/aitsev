import { promises as fs } from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { performRequest, validateRequest } from "../../src/main/proxy";
import { DEFAULT_SERVER, SettingsError, SettingsStore, validateServerUrl, type Encryptor } from "../../src/main/store";

const xor = (b: Buffer) => Buffer.from(b.map((x) => x ^ 0x5a));
const fakeEncryptor = (available = true): Encryptor => ({
  isEncryptionAvailable: () => available,
  encryptString: (t) => xor(Buffer.from(`enc:${t}`)),
  decryptString: (b) => xor(b).toString().replace(/^enc:/, ""),
});

let dir: string;
beforeEach(async () => {
  dir = await fs.mkdtemp(path.join(os.tmpdir(), "aitsev-desktop-"));
});
afterEach(async () => {
  await fs.rm(dir, { recursive: true, force: true });
});

describe("settings store", () => {
  it("defaults, saves and keeps the key encrypted on disk", async () => {
    const store = new SettingsStore(dir, fakeEncryptor());
    expect(await store.publicSettings()).toEqual({ serverUrl: DEFAULT_SERVER, hasApiKey: false });
    const saved = await store.save({ serverUrl: "https://aitsev.example.com/", apiKey: "  key-0123456789abcdef  " });
    expect(saved).toEqual({ serverUrl: "https://aitsev.example.com", hasApiKey: true });
    expect(await store.apiKey()).toBe("key-0123456789abcdef");
    const raw = await fs.readFile(path.join(dir, "api-key.bin"));
    expect(raw.toString()).not.toContain("key-0123456789abcdef");
    // saving without a key keeps the existing one; null removes it
    await store.save({ serverUrl: "http://localhost:9000" });
    expect(await store.apiKey()).toBe("key-0123456789abcdef");
    expect((await store.save({ serverUrl: "http://localhost:9000", apiKey: null })).hasApiKey).toBe(false);
  });

  it("refuses short keys and systems without a key store", async () => {
    await expect(new SettingsStore(dir, fakeEncryptor()).save({ serverUrl: DEFAULT_SERVER, apiKey: "short" })).rejects.toThrow(/16/);
    await expect(new SettingsStore(dir, fakeEncryptor(false)).save({ serverUrl: DEFAULT_SERVER, apiKey: "k".repeat(20) }))
      .rejects.toThrow(/secure key store/);
  });

  it.each([
    ["not a url", /full server URL/],
    ["ftp://x.example", /http/],
    ["http://aitsev.example.com", /https/],
    ["https://user:pw@aitsev.example.com", /credentials/],
  ])("rejects %s", (url, message) => {
    expect(() => validateServerUrl(url)).toThrow(SettingsError);
    expect(() => validateServerUrl(url)).toThrow(message);
  });

  it("allows plain http only for this computer", () => {
    expect(validateServerUrl("http://127.0.0.1:8000/")).toBe("http://127.0.0.1:8000");
    expect(validateServerUrl("http://localhost:8000")).toBe("http://localhost:8000");
  });
});

describe("request proxy", () => {
  it.each([
    [{ method: "GET", path: "/v1/connections" }, true],
    [{ method: "GET", path: "/health" }, true],
    [{ method: "GET", path: "/v1/jobs?mapping_id=abc-1" }, true],
    [{ method: "PUT", path: "/v1/mappings/x", body: { fields: [] } }, true],
    [{ method: "PATCH", path: "/v1/x" }, false],
    [{ method: "GET", path: "https://evil.example/" }, false],
    [{ method: "GET", path: "/v1/../admin" }, false],
    [{ method: "GET", path: "//evil.example/v1" }, false],
    [{ method: "GET", path: "/docs" }, false],
    [{ method: "GET", path: "/v1/x?a=<script>" }, false],
    [null, false],
  ])("validates %j", (raw, ok) => {
    if (ok) expect(() => validateRequest(raw)).not.toThrow();
    else expect(() => validateRequest(raw)).toThrow();
  });

  it("adds the key, sends JSON and parses responses", async () => {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    const fetchImpl = async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      return new Response(JSON.stringify({ id: "c1" }), { status: 201 });
    };
    const res = await performRequest({ method: "POST", path: "/v1/connections", body: { a: 1 } }, "https://s.example", "k".repeat(20), fetchImpl);
    expect(res).toEqual({ ok: true, status: 201, data: { id: "c1" } });
    const headers = calls[0]?.init.headers as Record<string, string>;
    expect(calls[0]?.url).toBe("https://s.example/v1/connections");
    expect(headers.Authorization).toBe(`Bearer ${"k".repeat(20)}`);
    expect(headers["Content-Type"]).toBe("application/json");
    const text = await performRequest({ method: "GET", path: "/v1/mappings/x/export" }, "https://s", "k", async () => new Response("print('hi')"));
    expect(text.data).toBe("print('hi')");
  });

  it("maps errors to readable messages", async () => {
    const reply = (status: number, body: unknown) => async () => new Response(JSON.stringify(body), { status });
    expect((await performRequest({ method: "GET", path: "/v1/x" }, "https://s", "k", reply(401, { detail: "Invalid API key" }))).error)
      .toBe("Invalid API key: Invalid API key");
    expect((await performRequest({ method: "POST", path: "/v1/x" }, "https://s", "k",
      reply(422, { detail: [{ loc: ["body", "base_url"], msg: "Input should be a valid URL" }] }))).error)
      .toBe("Error 422: base_url: Input should be a valid URL");
    const down = await performRequest({ method: "GET", path: "/v1/x" }, "https://s", "k", async () => { throw new TypeError("fetch failed"); });
    expect(down.error).toMatch(/Cannot reach the server/);
    const noKey = await performRequest({ method: "GET", path: "/v1/x" }, "https://s", null);
    expect(noKey.error).toMatch(/API key/);
  });
});

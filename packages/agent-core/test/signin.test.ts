import { describe, expect, it } from "vitest";
import { AitsevClient, type FetchLike } from "../src/client";
import { pollDeviceSignIn, SignInError, startDeviceSignIn } from "../src/signin";

const SERVER = "https://app.aitsev.com";
const START = { device_code: "dev_x", user_code: "BCDF-GHJK", verification_url: `${SERVER}/app/#/connect?code=BCDF-GHJK`, expires_in: 600, interval: 2 };

function fake(replies: Array<{ status: number; body?: unknown } | Error>) {
  const urls: string[] = [];
  const impl: FetchLike = async (url) => {
    urls.push(url);
    const next = replies.shift();
    if (!next) throw new Error("exhausted");
    if (next instanceof Error) throw next;
    return new Response(next.body === undefined ? "" : JSON.stringify(next.body), { status: next.status });
  };
  return { impl, urls };
}

describe("device sign-in", () => {
  it("starts and refuses approval pages on other origins", async () => {
    expect(await startDeviceSignIn(`${SERVER}/`, "Aitsev for Cursor", fake([{ status: 200, body: START }]).impl)).toEqual(START);
    await expect(startDeviceSignIn(SERVER, "x", fake([{ status: 200, body: { ...START, verification_url: "https://evil.example/x" } }]).impl))
      .rejects.toThrow(SignInError);
    await expect(startDeviceSignIn(SERVER, "x", fake([{ status: 500 }]).impl)).rejects.toThrow("could not start (500)");
    await expect(startDeviceSignIn(SERVER, "x", fake([new Error("down")]).impl)).rejects.toThrow(/Cannot reach/);
  });

  it("polls until approved and stops on errors, expiry and cancel", async () => {
    const ok = fake([{ status: 200, body: { status: "pending" } }, new Error("blip"), { status: 200, body: { status: "approved", api_key: "aitsev_k", email: "a@b.co" } }]);
    expect(await pollDeviceSignIn(SERVER, START, { fetch: ok.impl, sleep: async () => {} })).toEqual({ apiKey: "aitsev_k", email: "a@b.co" });
    expect(ok.urls.every((u) => u === `${SERVER}/v1/auth/device/poll`)).toBe(true);
    await expect(pollDeviceSignIn(SERVER, START, { fetch: fake([{ status: 400, body: { detail: "Sign-in was declined" } }]).impl, sleep: async () => {} }))
      .rejects.toThrow("declined");
    let t = 0;
    const pending = fake(Array.from({ length: 9 }, () => ({ status: 200, body: { status: "pending" } })));
    await expect(pollDeviceSignIn(SERVER, { ...START, expires_in: 4 }, { fetch: pending.impl, sleep: async () => {}, now: () => (t += 1000) }))
      .rejects.toThrow("expired");
    const abort = new AbortController();
    abort.abort();
    await expect(pollDeviceSignIn(SERVER, START, { fetch: pending.impl, signal: abort.signal })).rejects.toThrow("cancelled");
  });

  it("reads the model catalog", async () => {
    const { impl, urls } = fake([{ status: 200, body: { plan: "free", premium_allowed: false, max_selected: 3, upgrade_url: "u", models: [] } }]);
    const client = new AitsevClient({ baseUrl: SERVER, apiKey: "k", fetch: impl });
    expect((await client.catalog()).max_selected).toBe(3);
    expect(urls).toEqual([`${SERVER}/v1/models/catalog`]);
  });
});

describe("chat client", () => {
  it("posts chat turns and analysis requests", async () => {
    const seen: Array<{ url: string; body: unknown }> = [];
    const impl: FetchLike = async (url, init) => {
      seen.push({ url, body: JSON.parse(String(init?.body)) });
      const reply = url.endsWith("/v1/chat")
        ? { reply: "ok", model: "m", edits: [], skipped: [], verification: null, repaired: false, prompt_tokens: 1, completion_tokens: 1 }
        : { model: "m", suggestions: [] };
      return new Response(JSON.stringify(reply), { status: 200 });
    };
    const client = new AitsevClient({ baseUrl: "https://x.example", apiKey: "k", fetch: impl });
    expect((await client.chat({ messages: [{ role: "user", content: "hi" }], mode: "ask", files: {} })).reply).toBe("ok");
    expect((await client.analyze({ files: { "a.py": "x" } })).suggestions).toEqual([]);
    expect(seen.map((s) => s.url)).toEqual(["https://x.example/v1/chat", "https://x.example/v1/analyze"]);
  });
});

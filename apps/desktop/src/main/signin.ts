/**
 * Browser sign-in (device flow): the app asks the server for a short code, opens the browser on
 * the approval page, and polls until the user allows it. The server then hands this app its own
 * API key exactly once; the key is stored encrypted and never shown.
 */
import type { FetchLike } from "./proxy";

export interface SignInStart {
  device_code: string;
  user_code: string;
  verification_url: string;
  expires_in: number;
  interval: number;
}

export interface SignInResult {
  apiKey: string;
  email: string;
}

export class SignInError extends Error {
  override name = "SignInError";
}

async function post(fetchImpl: FetchLike, url: string, body: unknown): Promise<{ status: number; data: Record<string, unknown> }> {
  let res: Response;
  try {
    res = await fetchImpl(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(30_000),
    });
  } catch (err) {
    throw new SignInError(`Cannot reach the Aitsev server: ${err instanceof Error ? err.message : String(err)}`);
  }
  let data: Record<string, unknown> = {};
  try {
    data = (await res.json()) as Record<string, unknown>;
  } catch {
    /* empty or non-JSON body */
  }
  return { status: res.status, data };
}

/** The approval page must be on the same server the app talks to. */
export function safeVerificationUrl(serverUrl: string, raw: unknown): string {
  if (typeof raw !== "string") throw new SignInError("The server sent no sign-in page");
  const page = new URL(raw);
  const server = new URL(serverUrl);
  if (page.origin !== server.origin) {
    throw new SignInError(
      `The sign-in page (${page.origin}) is not on your Aitsev server (${server.origin}). ` +
        "Use the server's public address in Settings, or set AITSEV_PUBLIC_URL on the server.",
    );
  }
  return page.toString();
}

export async function startSignIn(serverUrl: string, clientName: string, fetchImpl: FetchLike): Promise<SignInStart> {
  const { status, data } = await post(fetchImpl, `${serverUrl}/v1/auth/device/start`, { client_name: clientName });
  if (status !== 200 || typeof data.device_code !== "string" || typeof data.user_code !== "string") {
    throw new SignInError(typeof data.detail === "string" ? data.detail : `Sign-in could not start (${status})`);
  }
  return {
    device_code: data.device_code,
    user_code: data.user_code,
    verification_url: safeVerificationUrl(serverUrl, data.verification_url),
    expires_in: Number(data.expires_in) || 600,
    interval: Math.max(1, Number(data.interval) || 3),
  };
}

export interface PollOptions {
  sleep?: (ms: number) => Promise<void>;
  signal?: AbortSignal;
  now?: () => number;
}

export async function pollSignIn(serverUrl: string, start: SignInStart, fetchImpl: FetchLike, options: PollOptions = {}): Promise<SignInResult> {
  const sleep = options.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  const now = options.now ?? Date.now;
  const deadline = now() + start.expires_in * 1000;
  while (now() < deadline) {
    if (options.signal?.aborted) throw new SignInError("Sign-in cancelled");
    await sleep(start.interval * 1000);
    if (options.signal?.aborted) throw new SignInError("Sign-in cancelled");
    let reply: { status: number; data: Record<string, unknown> };
    try {
      reply = await post(fetchImpl, `${serverUrl}/v1/auth/device/poll`, { device_code: start.device_code });
    } catch {
      continue; // brief network trouble: keep waiting until the code expires
    }
    const { status, data } = reply;
    if (status === 200 && data.status === "approved" && typeof data.api_key === "string") {
      return { apiKey: data.api_key, email: typeof data.email === "string" ? data.email : "" };
    }
    if (status === 200 || status === 429) continue;
    throw new SignInError(typeof data.detail === "string" ? data.detail : `Sign-in failed (${status})`);
  }
  throw new SignInError("The sign-in code expired. Try again.");
}

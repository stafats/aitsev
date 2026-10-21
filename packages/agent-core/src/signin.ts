/**
 * Browser sign-in for editors and apps (OAuth device-flow style): start, open the approval page
 * in the browser, then poll until the user allows it and the server returns a personal API key.
 */
import type { FetchLike } from "./client";

export interface DeviceStart {
  device_code: string;
  user_code: string;
  verification_url: string;
  expires_in: number;
  interval: number;
}

export interface DeviceGrant {
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
    /* empty body */
  }
  return { status: res.status, data };
}

export async function startDeviceSignIn(baseUrl: string, clientName: string, fetchImpl: FetchLike = (i, init) => fetch(i, init)): Promise<DeviceStart> {
  const server = baseUrl.replace(/\/+$/, "");
  const { status, data } = await post(fetchImpl, `${server}/v1/auth/device/start`, { client_name: clientName.slice(0, 60) });
  if (status !== 200 || typeof data.device_code !== "string" || typeof data.user_code !== "string" || typeof data.verification_url !== "string") {
    throw new SignInError(typeof data.detail === "string" ? data.detail : `Sign-in could not start (${status})`);
  }
  // Only ever open an approval page on the same server.
  const pageOrigin = new URL(data.verification_url).origin;
  if (pageOrigin !== new URL(server).origin) {
    throw new SignInError(
      `The sign-in page (${pageOrigin}) is not on your Aitsev server (${new URL(server).origin}). ` +
        "Use the server's public address in settings, or set AITSEV_PUBLIC_URL on the server.",
    );
  }
  return {
    device_code: data.device_code,
    user_code: data.user_code,
    verification_url: data.verification_url,
    expires_in: Number(data.expires_in) || 600,
    interval: Math.max(1, Number(data.interval) || 3),
  };
}

export interface PollOptions {
  fetch?: FetchLike;
  sleep?: (ms: number) => Promise<void>;
  signal?: AbortSignal;
  now?: () => number;
}

export async function pollDeviceSignIn(baseUrl: string, start: DeviceStart, options: PollOptions = {}): Promise<DeviceGrant> {
  const server = baseUrl.replace(/\/+$/, "");
  const fetchImpl = options.fetch ?? ((i, init) => fetch(i, init));
  const sleep = options.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  const now = options.now ?? Date.now;
  const deadline = now() + start.expires_in * 1000;
  while (now() < deadline) {
    if (options.signal?.aborted) throw new SignInError("Sign-in cancelled");
    await sleep(start.interval * 1000);
    if (options.signal?.aborted) throw new SignInError("Sign-in cancelled");
    let reply: { status: number; data: Record<string, unknown> };
    try {
      reply = await post(fetchImpl, `${server}/v1/auth/device/poll`, { device_code: start.device_code });
    } catch {
      continue;
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

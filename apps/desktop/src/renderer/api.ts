/** Typed access to the preload bridge. The API key never reaches this side. */

export interface ApiResponse<T = unknown> {
  ok: boolean;
  status: number;
  data: T;
  error?: string;
}

export interface SignInResult {
  ok: boolean;
  email?: string;
  error?: string;
}

export interface EditorInfo {
  id: string;
  name: string;
  found: boolean;
}

export interface InstallResult {
  id: string;
  name: string;
  ok: boolean;
  error?: string;
}

export interface Bridge {
  getSettings(): Promise<{ serverUrl: string; hasApiKey: boolean; email?: string }>;
  saveSettings(input: { serverUrl: string; apiKey?: string | null }): Promise<{
    ok: boolean;
    settings?: { serverUrl: string; hasApiKey: boolean; email?: string };
    error?: string;
  }>;
  request(method: string, path: string, body?: unknown): Promise<ApiResponse>;
  saveFile(defaultName: string, content: string): Promise<{ ok: boolean; path?: string; error?: string }>;
  openTextFile(): Promise<{ ok: boolean; name?: string; content?: string; error?: string }>;
  ready(): void;
  // Desktop only:
  signIn?(): Promise<{ ok: boolean; userCode?: string; verificationUrl?: string; error?: string }>;
  cancelSignIn?(): Promise<void>;
  reopenSignIn?(): Promise<void>;
  onSignInResult?(callback: (result: SignInResult) => void): () => void;
  listEditors?(): Promise<{ bundled: boolean; editors: EditorInfo[] }>;
  installEditors?(ids: string[]): Promise<InstallResult[]>;
}

declare global {
  interface Window {
    aitsev: Bridge;
  }
}

export class ApiError extends Error {
  override name = "ApiError";
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export function bridge(): Bridge {
  return window.aitsev;
}

export async function api<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await bridge().request(method, path, body);
  if (!res.ok) throw new ApiError(res.error ?? `Request failed (${res.status})`, res.status);
  return res.data as T;
}

// ------------------------------------------------------------------ server types

export interface Connection {
  id: string;
  name: string;
  base_url: string;
  auth: { type: string };
  has_credentials: boolean;
  spec_format: string;
  environment: string;
  platform?: string;
  entities: string[];
  discovered_at: string | null;
  last_error: string | null;
}

export interface FieldMapping {
  target: string;
  source: string | null;
  sources: string[];
  separator: string;
  constant: unknown;
  steps: Array<Record<string, unknown>>;
  confidence: number;
  method: string;
  status: "proposed" | "approved" | "rejected";
  reason: string;
}

export interface Mapping {
  id: string;
  name: string;
  status: string;
  source_connection_id: string;
  source_entity: string;
  target_connection_id: string;
  target_entity: string;
  fields: FieldMapping[];
  unmapped_targets: string[];
  blocking_issues: string[];
  models_used: string[];
  version: number;
  dry_run_passed: boolean;
  error: string | null;
}

export interface Job {
  id: string;
  mapping_id: string;
  dry_run: boolean;
  status: string;
  stats: Record<string, number>;
  errors: Array<{ index: number; source_key: unknown; stage: string; errors: string[] }>;
  sample: Array<Record<string, unknown>>;
  error: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface EntityReport {
  logical_name: string;
  display_name: string;
  total_records: number | null;
  sampled: number;
  fields: Array<{ name: string; display_name: string; type: string; custom: boolean; required: boolean; fill_rate: number }>;
  unused_custom_fields: string[];
  low_fill_fields: string[];
  required_but_empty: string[];
  duplicate_groups: Array<{ field: string; value: string; count: number }>;
  suggestions: string[];
}

export interface Analysis {
  id: string;
  connection_id: string;
  entities: string[];
  status: string;
  report: EntityReport[];
  error: string | null;
  created_at: string;
}

export interface FieldSpec {
  schema_name: string;
  display_name: string;
  description: string;
  type: string;
  required: string;
  max_length: number | null;
  min_value: number | null;
  max_value: number | null;
  precision: number | null;
  options: Array<{ label: string; value: number | null }>;
  true_label: string;
  false_label: string;
}

export interface CrmChange {
  id: string;
  connection_id: string;
  environment: string;
  entity: string;
  request: string;
  publisher_prefix: string;
  solution: string | null;
  status: string;
  items: Array<{ spec: FieldSpec; include: boolean; agreed: boolean; proposed_by: string[]; note: string; payload: unknown }>;
  results: Array<{ schema_name: string; status: string; message: string }>;
  version: number;
  error: string | null;
}

export interface TaskSummary {
  id: string;
  title: string;
  status: string;
  updated_at: string;
  error: string | null;
}

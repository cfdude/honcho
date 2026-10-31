import { Honcho } from "@honcho-ai/sdk";
import type { ErrorEvent } from "@sentry/cloudflare";
import pkg from "../package.json";

export const HEADER_HOST = "X-Honcho-Host";
export const HEADER_PLUGIN = "X-Honcho-Plugin";

/** Chat runs a tool-using agent that can outlast the SDK's 60s default. */
const DEFAULT_TIMEOUT_MS = 300_000;

/** Honcho's API caps client header values at 256 chars; match it. */
const MAX_PLUGIN_LEN = 256;

const HOST_VALUE = `honcho-mcp/${
  typeof pkg.version === "string" && pkg.version ? pkg.version : "unknown"
}`;

function clean(value: string | null | undefined): string | undefined {
  const v = value?.replace(/\s+/g, " ").trim();
  return v ? v.slice(0, MAX_PLUGIN_LEN) : undefined;
}

/**
 * Identity headers for every Honcho API request made on behalf of one MCP
 * request. Host names this server. Plugin is the caller's `X-Honcho-Plugin`
 * with its `User-Agent` in parentheses (`cursor-honcho/0.2.0 (Cursor/1.0.0)`),
 * or the `User-Agent` alone when the caller set no plugin, as before.
 */
export function identityHeaders(
  userAgent?: string | null,
  plugin?: string | null,
): Record<string, string> {
  const headers: Record<string, string> = { [HEADER_HOST]: HOST_VALUE };
  const ua = clean(userAgent);
  const name = clean(plugin);
  const value = name ? (ua ? `${name} (${ua})` : name) : ua;
  if (value) headers[HEADER_PLUGIN] = value.slice(0, MAX_PLUGIN_LEN);
  return headers;
}

export interface HonchoConfig {
  apiKey: string;
  baseUrl: string;
  /** From X-Honcho-Workspace-ID (HTTP) or HONCHO_WORKSPACE_ID (stdio). */
  workspaceId?: string;
  /**
   * FORK: extra headers sent on every upstream request. Empty unless the
   * operator has put the API behind Cloudflare Access — see `accessHeaders`.
   * Merged with upstream's identity headers at client construction, never
   * substituted for them.
   */
  extraHeaders?: Record<string, string>;
  /** Honcho API request timeout, from HONCHO_TIMEOUT_MS. */
  timeoutMs?: number;
}

export interface Env {
  HONCHO_API_URL?: string;
  HONCHO_TIMEOUT_MS?: string;
  ALERT_WEBHOOK_URL?: string;
  SENTRY_DSN?: string;
  /**
   * FORK: Cloudflare Access service-token pair, set as Worker secrets. Only
   * needed when HONCHO_API_URL points at a self-hosted instance fronted by
   * Cloudflare Access (an Access-protected hostname answers an unauthenticated
   * request with a 403 login page, not the API).
   */
  HONCHO_ACCESS_CLIENT_ID?: string;
  HONCHO_ACCESS_CLIENT_SECRET?: string;
}

export interface EnvConfig {
  HONCHO_API_KEY?: string;
  HONCHO_API_URL?: string;
  HONCHO_WORKSPACE_ID?: string;
  HONCHO_TIMEOUT_MS?: string;
  /** FORK: the stdio host needs the Access pair too — see accessHeaders. */
  HONCHO_ACCESS_CLIENT_ID?: string;
  HONCHO_ACCESS_CLIENT_SECRET?: string;
}

function parseTimeoutMs(value?: string): number | undefined {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : undefined;
}

/**
 * Cloudflare Access service-token headers, or `{}` when the pair is not
 * configured.
 *
 * Both halves are required: sending one alone is not a partial credential,
 * it is an invalid one, and Access rejects it exactly as it rejects none.
 * Returning `{}` in that case keeps the failure legible (a 403 that says
 * "no credentials") instead of masking a half-configured deployment.
 *
 * Deployments not behind Access set neither var and are unaffected.
 */
export function accessHeaders(
  env: Env | EnvConfig = {},
): Record<string, string> {
  const id = env.HONCHO_ACCESS_CLIENT_ID?.trim();
  const secret = env.HONCHO_ACCESS_CLIENT_SECRET?.trim();
  if (!id || !secret) return {};
  return { "CF-Access-Client-Id": id, "CF-Access-Client-Secret": secret };
}

/**
 * Parse configuration from request headers and Worker env bindings.
 * Throws only when the Authorization bearer token is missing/empty.
 *
 * The Honcho API URL is read from the `HONCHO_API_URL` env var when set,
 * allowing operators to run this Worker alongside a self-hosted Honcho
 * instance (see the "Self-Hosted Honcho" section in README.md). It is
 * intentionally not exposed as a request header: routing public requests
 * to an internal URL would be a latency and security regression.
 *
 * Optional `X-Honcho-Workspace-ID` becomes the default `workspace_id` on
 * tools. If the header is omitted, each tool call must pass `workspace_id`.
 */
export function parseConfig(request: Request, env: Env = {}): HonchoConfig {
  const authHeader = request.headers.get("Authorization");
  const bearerMatch = authHeader?.trim().match(/^Bearer\s+(.*)$/i);
  if (!bearerMatch) {
    throw new Error(
      "Missing Authorization header. Provide 'Authorization: Bearer <your-honcho-key>'.",
    );
  }
  const apiKey = bearerMatch[1].trim();
  if (!apiKey) {
    throw new Error("Authorization header is empty after 'Bearer '.");
  }

  const workspaceId =
    request.headers.get("X-Honcho-Workspace-ID")?.trim() || undefined;

  return {
    apiKey,
    baseUrl: env.HONCHO_API_URL?.trim() || "https://api.honcho.dev",
    workspaceId,
    extraHeaders: accessHeaders(env),
    timeoutMs: parseTimeoutMs(env.HONCHO_TIMEOUT_MS),
  };
}

/** Parse configuration from process env. */
export function parseEnvConfig(env: EnvConfig): HonchoConfig {
  const apiKey = env.HONCHO_API_KEY?.trim();
  if (!apiKey) {
    throw new Error(
      "Missing HONCHO_API_KEY. Set HONCHO_API_KEY to your Honcho API key.",
    );
  }
  return {
    apiKey,
    baseUrl: env.HONCHO_API_URL?.trim() || "https://api.honcho.dev",
    workspaceId: env.HONCHO_WORKSPACE_ID?.trim() || undefined,
    // FORK: the stdio host reaches the same Access-fronted API as the Worker,
    // so it needs the service token too. Upstream's parseEnvConfig is new in
    // this merge and had no notion of it.
    extraHeaders: accessHeaders(env),
    timeoutMs: parseTimeoutMs(env.HONCHO_TIMEOUT_MS),
  };
}

export const MISSING_WORKSPACE_ID_MESSAGE =
  "Missing workspace_id. Pass workspace_id on the next tool call, or set X-Honcho-Workspace-ID (HTTP) / HONCHO_WORKSPACE_ID (stdio).";

export function resolveWorkspaceId(
  config: HonchoConfig,
  workspaceId?: string,
): string {
  const id = workspaceId?.trim() || config.workspaceId?.trim();
  if (!id) {
    throw new Error(MISSING_WORKSPACE_ID_MESSAGE);
  }
  return id;
}

export function createClient(
  config: HonchoConfig,
  workspaceId: string,
  headers: Record<string, string> = identityHeaders(),
): Honcho {
  return new Honcho({
    apiKey: config.apiKey,
    baseURL: config.baseUrl,
    workspaceId,
    timeout: config.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    // FORK: upstream's identity headers MERGED with our Cloudflare Access
    // service token, not replaced by it. Taking either side alone breaks
    // something: without `headers` the upstream plugin/identity headers vanish,
    // and without extraHeaders an Access-fronted API answers 403 with an HTML
    // login page. Ours spreads last; the key spaces are disjoint
    // (CF-Access-* vs identity), so the order is for determinism, not override.
    defaultHeaders: { ...headers, ...(config.extraHeaders ?? {}) },
  });
}

/** Client used only for credential-scoped ops (list workspaces). */
export function createUnscopedClient(
  config: HonchoConfig,
  headers: Record<string, string> = identityHeaders(),
): Honcho {
  return new Honcho({
    apiKey: config.apiKey,
    baseURL: config.baseUrl,
    timeout: config.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    // FORK: upstream's identity headers MERGED with our Cloudflare Access
    // service token, not replaced by it. Taking either side alone breaks
    // something: without `headers` the upstream plugin/identity headers vanish,
    // and without extraHeaders an Access-fronted API answers 403 with an HTML
    // login page. Ours spreads last; the key spaces are disjoint
    // (CF-Access-* vs identity), so the order is for determinism, not override.
    defaultHeaders: { ...headers, ...(config.extraHeaders ?? {}) },
  });
}

export function createClientFactory(
  config: HonchoConfig,
  headers: Record<string, string> = identityHeaders(),
): (workspaceId?: string) => Honcho {
  const cache = new Map<string, Honcho>();
  return (workspaceId?: string) => {
    const id = resolveWorkspaceId(config, workspaceId);
    let client = cache.get(id);
    if (!client) {
      client = createClient(config, id, headers);
      cache.set(id, client);
    }
    return client;
  };
}

/** Both Honcho clients for one MCP request, sharing one identity header set. */
export function honchoClients(
  config: HonchoConfig,
  headers: Record<string, string> = identityHeaders(),
): { clientFor: (workspaceId?: string) => Honcho; unscoped: Honcho } {
  return {
    clientFor: createClientFactory(config, headers),
    unscoped: createUnscopedClient(config, headers),
  };
}

/**
 * Sentry options shared by every entry point. Without SENTRY_DSN the SDK stays disabled and
 * every capture is a no-op. Requests carry the caller's Honcho key
 * (Authorization) and memory content (body), so events keep only the method
 * and URL of the request.
 */
export function sentryOptions(env: Env) {
  return {
    dsn: env.SENTRY_DSN,
    beforeSend(event: ErrorEvent) {
      if (event.request) {
        event.request = { method: event.request.method, url: event.request.url };
      }
      return event;
    },
  };
}

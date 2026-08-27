import { Honcho } from "@honcho-ai/sdk";

export interface HonchoConfig {
  apiKey: string;
  baseUrl: string;
  /** From X-Honcho-Workspace-ID when set. */
  workspaceId?: string;
  /**
   * Extra headers sent on every upstream request. Empty unless the operator
   * has put the API behind Cloudflare Access — see `accessHeaders`.
   */
  extraHeaders?: Record<string, string>;
}

export interface Env {
  HONCHO_API_URL?: string;
  ALERT_WEBHOOK_URL?: string;
  /**
   * Cloudflare Access service-token pair, set as Worker secrets. Only needed
   * when HONCHO_API_URL points at a self-hosted instance fronted by
   * Cloudflare Access (an Access-protected hostname answers an unauthenticated
   * request with a 403 login page, not the API).
   */
  HONCHO_ACCESS_CLIENT_ID?: string;
  HONCHO_ACCESS_CLIENT_SECRET?: string;
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
export function accessHeaders(env: Env = {}): Record<string, string> {
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
  };
}

export const MISSING_WORKSPACE_ID_MESSAGE =
  "Missing workspace_id. Pass workspace_id on the next tool call, or set the X-Honcho-Workspace-ID header on the connection so it is used automatically.";

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
): Honcho {
  return new Honcho({
    apiKey: config.apiKey,
    baseURL: config.baseUrl,
    workspaceId,
    ...(config.extraHeaders && Object.keys(config.extraHeaders).length > 0
      ? { defaultHeaders: config.extraHeaders }
      : {}),
  });
}

/** Client used only for credential-scoped ops (list workspaces). */
export function createUnscopedClient(config: HonchoConfig): Honcho {
  return new Honcho({
    apiKey: config.apiKey,
    baseURL: config.baseUrl,
    ...(config.extraHeaders && Object.keys(config.extraHeaders).length > 0
      ? { defaultHeaders: config.extraHeaders }
      : {}),
  });
}

export function createClientFactory(
  config: HonchoConfig,
): (workspaceId?: string) => Honcho {
  const cache = new Map<string, Honcho>();
  return (workspaceId?: string) => {
    const id = resolveWorkspaceId(config, workspaceId);
    let client = cache.get(id);
    if (!client) {
      client = createClient(config, id);
      cache.set(id, client);
    }
    return client;
  };
}

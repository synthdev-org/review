// apps/admin_api/frontend's one HTTP client — every screen's data fetching
// goes through this module, never a bare `fetch` call, so the CSRF-header
// attachment and error-shape normalization live in exactly
// one place.
//
// Cookies (`review_admin_session`, `review_admin_csrf`) are same-origin —
// the built frontend is served by the same apps/admin_api process it
// calls — so `credentials: "same-origin"` (the fetch
// default) already carries them; no CORS configuration is needed.

const CSRF_COOKIE_NAME = "review_admin_csrf";
const CSRF_HEADER_NAME = "X-CSRF-Token";

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

/** True when this response failed because the caller's session is
 * missing/expired (401) — every screen's data hook uses this to route back
 * to `/login`, per the PRD's "session expiring mid-action returns the user
 * to login" edge case. */
export function isSessionExpiredError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

function readCookie(name: string): string | null {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(";")) {
    const trimmed = part.trim();
    if (trimmed.startsWith(prefix)) {
      return decodeURIComponent(trimmed.slice(prefix.length));
    }
  }
  return null;
}

async function parseErrorDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") {
      return body.detail;
    }
  } catch {
    // Response body wasn't JSON (or was empty) — fall through to a generic
    // message rather than letting a parse error mask the real HTTP status.
  }
  return `request failed with status ${response.status}`;
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
}

/**
 * Performs one `/api/v1/...` request. Throws `ApiError` on any non-2xx
 * response (including a network-unreachable failure, normalized to status
 * `0`) — callers never need to branch on `response.ok` themselves.
 */
export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? "GET";
  const headers: Record<string, string> = {};
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (!SAFE_METHODS.has(method)) {
    const csrfToken = readCookie(CSRF_COOKIE_NAME);
    if (csrfToken) {
      headers[CSRF_HEADER_NAME] = csrfToken;
    }
  }

  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, {
      method,
      headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
      credentials: "same-origin",
      signal: options.signal,
    });
  } catch {
    // Distinct from a validation/auth error — the PRD's "couldn't reach the
    // server, retry" state, never an indefinite spinner.
    throw new ApiError(0, "could not reach the server — check your connection and retry");
  }

  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorDetail(response));
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

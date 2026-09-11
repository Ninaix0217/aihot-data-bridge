import { SchedulerError, SchedulerErrorReason } from "./types";

export const GITHUB_API_ROOT = "https://api.github.com";
export const GITHUB_API_VERSION = "2026-03-10";
export const MAX_TRANSPORT_ATTEMPTS = 3;
const REQUEST_TIMEOUT_MS = 10_000;

export interface GitHubRequestOptions {
  token: string;
  method: "GET" | "POST" | "PUT";
  path: string;
  body?: unknown;
  expectedStatus: number;
}

export async function githubRequest(
  options: GitHubRequestOptions,
  fetcher: typeof fetch,
  sleep: (milliseconds: number) => Promise<void>,
): Promise<Response> {
  let lastFailure: string | undefined;
  for (let attempt = 1; attempt <= MAX_TRANSPORT_ATTEMPTS; attempt += 1) {
    let response: Response;
    try {
      const request: RequestInit = {
        method: options.method,
        headers: {
          Accept: "application/vnd.github+json",
          Authorization: `Bearer ${options.token}`,
          "X-GitHub-Api-Version": GITHUB_API_VERSION,
          "Content-Type": "application/json",
        },
        signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      };
      if (options.body !== undefined) request.body = JSON.stringify(options.body);
      response = await fetcher(`${GITHUB_API_ROOT}${options.path}`, request);
    } catch {
      lastFailure = "network or timeout failure";
      if (attempt < MAX_TRANSPORT_ATTEMPTS) {
        await sleep(100 * 2 ** (attempt - 1));
        continue;
      }
      throw new SchedulerError(
        SchedulerErrorReason.GITHUB_API_TRANSIENT_FAILURE,
        `${options.method} ${options.path} exhausted bounded transport retry`,
      );
    }

    if (response.status === options.expectedStatus) return response;
    if (response.status === 429 || response.status >= 500) {
      lastFailure = `HTTP ${response.status}`;
      if (attempt < MAX_TRANSPORT_ATTEMPTS) {
        await sleep(retryDelay(response, attempt));
        continue;
      }
      throw new SchedulerError(
        SchedulerErrorReason.GITHUB_API_TRANSIENT_FAILURE,
        `${options.method} ${options.path} exhausted bounded retry after ${lastFailure}`,
        response.status,
      );
    }
    if (response.status === 401 || response.status === 403) {
      throw new SchedulerError(
        SchedulerErrorReason.GITHUB_API_UNAUTHORIZED,
        `${options.method} ${options.path} returned HTTP ${response.status}`,
        response.status,
      );
    }
    if (response.status === 404) {
      throw new SchedulerError(
        SchedulerErrorReason.GITHUB_API_NOT_FOUND,
        `${options.method} ${options.path} returned HTTP 404`,
        404,
      );
    }
    throw new SchedulerError(
      SchedulerErrorReason.CONTROL_WRITE_FAILED,
      `${options.method} ${options.path} returned HTTP ${response.status}`,
      response.status,
    );
  }
  throw new SchedulerError(
    SchedulerErrorReason.GITHUB_API_TRANSIENT_FAILURE,
    lastFailure ?? "unreachable bounded retry state",
  );
}

export async function responseJson(response: Response): Promise<Record<string, unknown>> {
  let value: unknown;
  try {
    value = await response.json();
  } catch {
    throw new SchedulerError(
      SchedulerErrorReason.GITHUB_API_MALFORMED_RESPONSE,
      "GitHub response was not valid JSON",
    );
  }
  if (!isRecord(value)) {
    throw new SchedulerError(
      SchedulerErrorReason.GITHUB_API_MALFORMED_RESPONSE,
      "GitHub response root was not an object",
    );
  }
  return value;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function retryDelay(response: Response, attempt: number): number {
  const raw = response.headers.get("Retry-After");
  if (raw && /^\d+$/.test(raw)) return Math.min(Number(raw) * 1000, 2_000);
  return 100 * 2 ** (attempt - 1);
}

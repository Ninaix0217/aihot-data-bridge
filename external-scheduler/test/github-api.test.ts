import { describe, expect, it, vi } from "vitest";

import { GITHUB_API_VERSION, GITHUB_USER_AGENT, githubRequest } from "../src/github-api";
import { exchangeInstallationToken } from "../src/github-app";
import { SchedulerErrorReason } from "../src/types";
import { NOW } from "./helpers";

const TOKEN = "synthetic-token-never-a-real-credential";
const BODY = { repositories: ["aihot-data-bridge"], permissions: { contents: "write" } };
const sleep = async () => undefined;

describe("shared GitHub HTTP contract", () => {
  it.each(["GET", "POST", "PUT"] as const)("preserves %s headers and serialization", async (method) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 200 }));
    const options = { token: TOKEN, method, path: "/synthetic", expectedStatus: 200 };
    await githubRequest(method === "GET" ? options : { ...options, body: BODY }, fetcher, sleep);
    const [url, init] = fetcher.mock.calls[0]!;
    expect(url).toBe("https://api.github.com/synthetic");
    expect(init?.method).toBe(method);
    expect(init?.headers).toEqual({
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${TOKEN}`,
      "X-GitHub-Api-Version": "2026-03-10",
      "Content-Type": "application/json",
      "User-Agent": "aihot-v2-independent-recovery-scheduler/1.0",
    });
    expect(GITHUB_API_VERSION).toBe("2026-03-10");
    expect(init?.body).toBe(method === "GET" ? undefined : JSON.stringify(BODY));
  });

  it("keeps User-Agent fixed and independent of authentication and request data", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 200 }));
    for (const token of [TOKEN, "another-synthetic-jwt"]) {
      await githubRequest({ token, method: "POST", path: "/synthetic", body: { request_id: token }, expectedStatus: 200 }, fetcher, sleep);
    }
    expect(GITHUB_USER_AGENT).toBe("aihot-v2-independent-recovery-scheduler/1.0");
    for (const [, init] of fetcher.mock.calls) {
      expect(new Headers(init?.headers).get("User-Agent")).toBe(GITHUB_USER_AGENT);
    }
  });

  it("exchanges the unchanged scoped token body against a User-Agent-required endpoint", async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (_url, init) => {
      if (new Headers(init?.headers).get("User-Agent") !== GITHUB_USER_AGENT) {
        return new Response("User-Agent required", { status: 403 });
      }
      return Response.json({ token: TOKEN, expires_at: "2026-09-11T09:00:00Z", permissions: { contents: "write" } }, { status: 201 });
    });
    const url = "https://api.github.com/app/installations/12345/access_tokens";
    expect((await fetcher(url, { method: "POST", headers: { Authorization: `Bearer ${TOKEN}` }, body: JSON.stringify(BODY) })).status).toBe(403);
    fetcher.mockClear();
    await expect(exchangeInstallationToken("12345", TOKEN, NOW, fetcher, sleep)).resolves.toEqual({ token: TOKEN, expiresAt: "2026-09-11T09:00:00Z" });
    expect(fetcher).toHaveBeenCalledTimes(1);
    const [actualUrl, init] = fetcher.mock.calls[0]!;
    expect(actualUrl).toBe(url);
    expect(init?.method).toBe("POST");
    expect(init?.body).toBe(JSON.stringify(BODY));
  });

  it("preserves bounded HTTP retry, delay, and request headers/body on every attempt", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(null, { status: 429, headers: { "Retry-After": "3" } }))
      .mockResolvedValueOnce(new Response(null, { status: 503 }))
      .mockResolvedValueOnce(new Response(null, { status: 201 }));
    const delays: number[] = [];
    await githubRequest({ token: TOKEN, method: "POST", path: "/synthetic", body: BODY, expectedStatus: 201 }, fetcher, async (ms) => { delays.push(ms); });
    expect(fetcher).toHaveBeenCalledTimes(3);
    expect(delays).toEqual([2000, 200]);
    for (const [, init] of fetcher.mock.calls) {
      expect(new Headers(init?.headers).get("User-Agent")).toBe(GITHUB_USER_AGENT);
      expect(new Headers(init?.headers).get("Authorization")).toBe(`Bearer ${TOKEN}`);
      expect(init?.body).toBe(JSON.stringify(BODY));
    }
  });

  it("preserves network retry bounds and delays", async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new Error("synthetic network failure"));
    const delays: number[] = [];
    await expect(githubRequest({ token: TOKEN, method: "GET", path: "/synthetic", expectedStatus: 200 }, fetcher, async (ms) => { delays.push(ms); })).rejects.toMatchObject({ reason: SchedulerErrorReason.GITHUB_API_TRANSIENT_FAILURE });
    expect(fetcher).toHaveBeenCalledTimes(3);
    expect(delays).toEqual([100, 200]);
  });

  it.each([401, 403])("preserves fail-closed %s without retry", async (status) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status }));
    const sleeper = vi.fn(sleep);
    await expect(githubRequest({ token: TOKEN, method: "GET", path: "/synthetic", expectedStatus: 200 }, fetcher, sleeper)).rejects.toMatchObject({ reason: SchedulerErrorReason.GITHUB_API_UNAUTHORIZED, status });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(sleeper).not.toHaveBeenCalled();
  });
});

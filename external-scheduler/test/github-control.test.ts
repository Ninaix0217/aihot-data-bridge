import { describe, expect, it, vi } from "vitest";

import {
  CONTROL_BRANCH,
  CONTROL_PATH,
  GITHUB_OWNER,
  GITHUB_REPOSITORY,
  GitHubControlClient,
} from "../src/github-control";
import { type ExternalTriggerPayload, SchedulerErrorReason } from "../src/types";
import {
  COMMIT_SHA,
  contentResponse,
  gitBlobSha,
  NEW_BLOB_SHA,
  NOW,
  OLD_BLOB_SHA,
  toBase64,
} from "./helpers";

const encoder = new TextEncoder();

function trigger(requestId = "2026-09-11/cf-1"): ExternalTriggerPayload {
  return {
    schema_version: "aihot-external-trigger/v1",
    enabled: true,
    target_report_date: "2026-09-11",
    mode: "RECOVERY",
    request_id: requestId,
    requested_at: NOW.toISOString(),
    source: "cloudflare-worker",
  };
}

async function currentResponse(payload: unknown): Promise<Response> {
  const bytes = encoder.encode(`${JSON.stringify(payload)}\n`);
  return contentResponse(bytes, await gitBlobSha(bytes));
}

describe("fixed GitHub control target", () => {
  it("cannot be redirected to repository, branch, path, Actions, or snapshot-data", () => {
    expect(GITHUB_OWNER).toBe("Ninaix0217");
    expect(GITHUB_REPOSITORY).toBe("aihot-data-bridge");
    expect(CONTROL_BRANCH).toBe("aihot-scheduler-control");
    expect(CONTROL_PATH).toBe(".aihot-control/trigger.json");
  });
});

describe("control read and conditional update", () => {
  it("validates and parses a current control file", async () => {
    const response = await currentResponse({
      schema_version: "aihot-external-trigger/v1",
      enabled: false,
      kind: "BOOTSTRAP",
    });
    const client = clientFor(vi.fn<typeof fetch>().mockResolvedValue(response));
    const current = await client.read();
    expect(current.payload).toMatchObject({ enabled: false, kind: "BOOTSTRAP" });
    expect(current.blobSha).toMatch(/^[0-9a-f]{40}$/);
  });

  it("fails closed on permanent 404 without retry", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response("missing", { status: 404 }));
    await expect(clientFor(fetcher).read()).rejects.toMatchObject({
      reason: SchedulerErrorReason.GITHUB_API_NOT_FOUND,
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("fails closed on malformed current JSON", async () => {
    const bytes = encoder.encode("not-json");
    const response = contentResponse(bytes, await gitBlobSha(bytes));
    await expect(clientFor(vi.fn<typeof fetch>().mockResolvedValue(response)).read()).rejects.toMatchObject({
      reason: SchedulerErrorReason.CONTROL_STATE_INVALID,
    });
  });

  it("fails closed on a valid JSON object with malformed control semantics", async () => {
    await expect(
      clientFor(vi.fn<typeof fetch>().mockResolvedValue(
        await currentResponse({ request_id: "untrusted-only" }),
      )).read(),
    ).rejects.toMatchObject({ reason: SchedulerErrorReason.CONTROL_STATE_INVALID });
  });

  it("does not PUT when request_id is already current", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      await currentResponse(trigger()),
    );
    const client = clientFor(fetcher);
    const current = await client.read();
    const result = await client.update(current, trigger());
    expect(result.result).toBe("NOOP_DUPLICATE");
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("PUTs only fixed payload with the current blob SHA", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(await currentResponse(trigger("2026-09-10/cf-old")))
      .mockResolvedValueOnce(Response.json({
        content: { sha: NEW_BLOB_SHA },
        commit: { sha: COMMIT_SHA },
      }));
    const client = clientFor(fetcher);
    const current = await client.read();
    const result = await client.update(current, trigger());
    expect(result).toMatchObject({ result: "WRITTEN", newBlobSha: NEW_BLOB_SHA, commitSha: COMMIT_SHA });
    const [url, init] = fetcher.mock.calls[1]!;
    expect(url).toBe(`https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPOSITORY}/contents/.aihot-control/trigger.json`);
    const body = JSON.parse(String(init?.body));
    expect(body.branch).toBe(CONTROL_BRANCH);
    expect(body.sha).toBe(current.blobSha);
    expect(JSON.parse(atob(body.content))).toEqual(trigger());
    expect(String(url)).not.toContain("snapshot-data");
    expect(String(url)).not.toContain("actions/workflows");
  });

  it.each([409, 422])("maps a %s race that landed the same request to NOOP", async (status) => {
    const old = await currentResponse(trigger("2026-09-10/cf-old"));
    const landed = await currentResponse(trigger());
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(old)
      .mockResolvedValueOnce(new Response("conflict", { status }))
      .mockResolvedValueOnce(landed);
    const client = clientFor(fetcher);
    expect((await client.update(await client.read(), trigger())).result).toBe("NOOP_DUPLICATE");
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it.each([409, 422])("fails closed when a %s race lands a different request", async (status) => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(await currentResponse(trigger("2026-09-10/cf-old")))
      .mockResolvedValueOnce(new Response("conflict", { status }))
      .mockResolvedValueOnce(await currentResponse(trigger("2026-09-11/cf-other")));
    const client = clientFor(fetcher);
    await expect(client.update(await client.read(), trigger())).rejects.toMatchObject({
      reason: SchedulerErrorReason.CONTROL_CONCURRENT_UPDATE,
    });
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it.each([401, 403])("does not retry authorization failure %s", async (status) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response("no", { status }));
    await expect(clientFor(fetcher).read()).rejects.toMatchObject({
      reason: SchedulerErrorReason.GITHUB_API_UNAUTHORIZED,
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("bounds 429/5xx retry and succeeds on the final attempt", async () => {
    const sleeps: number[] = [];
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response("busy", { status: 429, headers: { "Retry-After": "3" } }))
      .mockResolvedValueOnce(new Response("bad", { status: 500 }))
      .mockResolvedValueOnce(await currentResponse(trigger("2026-09-10/cf-old")));
    const current = await new GitHubControlClient(
      "installation-token",
      fetcher,
      async (milliseconds) => { sleeps.push(milliseconds); },
    ).read();
    expect(current.payload.request_id).toBe("2026-09-10/cf-old");
    expect(fetcher).toHaveBeenCalledTimes(3);
    expect(sleeps).toEqual([2_000, 200]);
  });

  it("bounds network/timeout failures", async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new Error("network includes no token"));
    await expect(clientFor(fetcher).read()).rejects.toMatchObject({
      reason: SchedulerErrorReason.GITHUB_API_TRANSIENT_FAILURE,
    });
    expect(fetcher).toHaveBeenCalledTimes(3);
  });
});

function clientFor(fetcher: typeof fetch): GitHubControlClient {
  return new GitHubControlClient("installation-token", fetcher, async () => undefined);
}

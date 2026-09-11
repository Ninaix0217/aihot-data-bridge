import {
  createExecutionContext,
  createScheduledController,
  waitOnExecutionContext,
} from "cloudflare:test";
import { afterEach, describe, expect, it, vi } from "vitest";

import worker from "../src/index";
import type { SchedulerEnv } from "../src/types";
import { contentResponse, gitBlobSha, SCHEDULED_TIME, testPrivateKeyPem } from "./helpers";

const encoder = new TextEncoder();

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("official Cloudflare scheduled runtime contract", () => {
  it("uses an explicit ScheduledController time and never logs credentials", async () => {
    const privateKey = await testPrivateKeyPem();
    const currentBytes = encoder.encode('{"schema_version":"aihot-external-trigger/v1","enabled":false,"kind":"BOOTSTRAP"}\n');
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url.includes("/app/installations/12345/access_tokens")) {
        return Response.json({
          token: "installation-secret-token",
          expires_at: "2099-01-01T00:00:00Z",
          permissions: { contents: "write" },
        }, { status: 201 });
      }
      if (init?.method === "GET") {
        return contentResponse(currentBytes, await gitBlobSha(currentBytes));
      }
      if (init?.method === "PUT") {
        return Response.json({
          content: { sha: "b".repeat(40) },
          commit: { sha: "c".repeat(40) },
        });
      }
      throw new Error(`unexpected test request ${url}`);
    });
    vi.stubGlobal("fetch", fetcher);
    const log = vi.spyOn(console, "log").mockImplementation(() => undefined);
    const controller = createScheduledController({
      scheduledTime: new Date(SCHEDULED_TIME),
      cron: "25 5 * * *",
    });
    const context = createExecutionContext();
    const env: SchedulerEnv = {
      GITHUB_APP_CLIENT_ID: "Iv1.runtime-test",
      GITHUB_INSTALLATION_ID: "12345",
      GITHUB_APP_PRIVATE_KEY: privateKey,
      DEPLOYMENT_MODE: "production",
    };

    await worker.scheduled(controller, env, context);
    await waitOnExecutionContext(context);

    const put = fetcher.mock.calls.find(([, init]) => init?.method === "PUT");
    expect(put).toBeTruthy();
    const body = JSON.parse(String(put![1]?.body));
    const payload = JSON.parse(atob(body.content));
    expect(payload.target_report_date).toBe("2026-09-11");
    expect(payload.request_id).toBe(`2026-09-11/cf-${SCHEDULED_TIME}`);
    const logged = log.mock.calls.map((call) => JSON.stringify(call)).join("\n");
    expect(logged).not.toContain(privateKey);
    expect(logged).not.toContain("installation-secret-token");
    expect(logged).not.toContain("eyJ");
  });
});

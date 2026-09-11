import { describe, expect, it, vi } from "vitest";

import { runScheduledRecovery } from "../src/index";
import type { RuntimeDependencies, SchedulerEnv } from "../src/types";
import { contentResponse, gitBlobSha, NOW, SCHEDULED_TIME, testPrivateKeyPem } from "./helpers";

const encoder = new TextEncoder();

describe("scheduler orchestration", () => {
  it("reports duplicate without control PUT and redacts all credentials", async () => {
    const privateKey = await testPrivateKeyPem();
    const requestId = `2026-09-11/cf-${SCHEDULED_TIME}`;
    const bytes = encoder.encode(`${JSON.stringify({
      schema_version: "aihot-external-trigger/v1",
      enabled: true,
      target_report_date: "2026-09-11",
      mode: "RECOVERY",
      request_id: requestId,
      requested_at: NOW.toISOString(),
      source: "cloudflare-worker",
    })}\n`);
    const logs: Record<string, unknown>[] = [];
    const fetcher = vi.fn<typeof fetch>(async (_input, init) => {
      if (init?.method === "POST") {
        return Response.json({
          token: "installation-secret-token",
          expires_at: "2026-09-11T09:00:00Z",
          permissions: { contents: "write" },
        }, { status: 201 });
      }
      return contentResponse(bytes, await gitBlobSha(bytes));
    });
    const dependencies: RuntimeDependencies = {
      fetch: fetcher,
      now: () => NOW,
      sleep: async () => undefined,
      log: (entry) => { logs.push(entry); },
    };
    const env: SchedulerEnv = {
      GITHUB_APP_CLIENT_ID: "Iv1.test-client",
      GITHUB_INSTALLATION_ID: "12345",
      GITHUB_APP_PRIVATE_KEY: privateKey,
      DEPLOYMENT_MODE: "production",
    };

    const result = await runScheduledRecovery(
      { scheduledTime: SCHEDULED_TIME, cron: "25 5 * * *" },
      env,
      dependencies,
    );
    expect(result.result).toBe("NOOP_DUPLICATE");
    expect(fetcher.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(0);
    const rendered = JSON.stringify(logs);
    expect(rendered).not.toContain(privateKey);
    expect(rendered).not.toContain("installation-secret-token");
    expect(rendered).not.toContain("eyJ");
  });
});

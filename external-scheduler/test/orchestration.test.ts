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

  it("writes the first probe occurrence and no-ops later occurrences with the same probe ID", async () => {
    const privateKey = await testPrivateKeyPem();
    let currentBytes = encoder.encode(
      '{"schema_version":"aihot-external-trigger/v1","enabled":false,"kind":"BOOTSTRAP"}\n',
    );
    const fetcher = vi.fn<typeof fetch>(async (_input, init) => {
      if (init?.method === "POST") {
        return Response.json({
          token: "installation-secret-token",
          expires_at: "2026-09-11T09:00:00Z",
          permissions: { contents: "write" },
        }, { status: 201 });
      }
      if (init?.method === "GET") {
        return contentResponse(currentBytes, await gitBlobSha(currentBytes));
      }
      if (init?.method === "PUT") {
        const body = JSON.parse(String(init.body));
        currentBytes = Uint8Array.from(atob(body.content), (character) =>
          character.charCodeAt(0),
        );
        return Response.json({
          content: { sha: await gitBlobSha(currentBytes) },
          commit: { sha: "c".repeat(40) },
        });
      }
      throw new Error("unexpected request");
    });
    const dependencies: RuntimeDependencies = {
      fetch: fetcher,
      now: () => NOW,
      sleep: async () => undefined,
      log: () => undefined,
    };
    const probeRequestId =
      "2026-09-10/cf-probe-123e4567-e89b-12d3-a456-426614174000";
    const env: SchedulerEnv = {
      GITHUB_APP_CLIENT_ID: "Iv1.test-client",
      GITHUB_INSTALLATION_ID: "12345",
      GITHUB_APP_PRIVATE_KEY: privateKey,
      DEPLOYMENT_MODE: "probe",
      PROBE_TARGET_REPORT_DATE: "2026-09-10",
      PROBE_REQUEST_ID: probeRequestId,
    };

    const first = await runScheduledRecovery(
      { scheduledTime: SCHEDULED_TIME, cron: "*/5 * * * *" },
      env,
      dependencies,
    );
    const second = await runScheduledRecovery(
      { scheduledTime: SCHEDULED_TIME + 300_000, cron: "*/5 * * * *" },
      env,
      dependencies,
    );

    expect(first.result).toBe("WRITTEN");
    expect(second.result).toBe("NOOP_DUPLICATE");
    expect(first.request_id).toBe(probeRequestId);
    expect(second.request_id).toBe(probeRequestId);
    expect(first.scheduled_at).not.toBe(second.scheduled_at);
    expect(fetcher.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(1);
  });
});

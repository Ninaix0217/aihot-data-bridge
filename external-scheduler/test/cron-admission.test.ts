import { afterEach, describe, expect, it, vi } from "vitest";

import * as githubApp from "../src/github-app";
import { runScheduledRecovery } from "../src/index";
import { admitScheduledCron, PRODUCTION_CRON } from "../src/scheduler";
import { type SchedulerEnv, SchedulerErrorReason } from "../src/types";
import { NOW, SCHEDULED_TIME } from "./helpers";

const production: SchedulerEnv = {
  DEPLOYMENT_MODE: "production",
  GITHUB_APP_CLIENT_ID: "synthetic-client",
  GITHUB_INSTALLATION_ID: "12345",
  GITHUB_APP_PRIVATE_KEY: "invalid-test-key-must-never-be-read",
};
const probe: SchedulerEnv = {
  ...production,
  DEPLOYMENT_MODE: "probe",
  PROBE_ALLOWED_CRON: "* * * * *",
  PROBE_TARGET_REPORT_DATE: "2026-09-10",
  PROBE_REQUEST_ID: "2026-09-10/cf-probe-synthetic",
};

afterEach(() => vi.restoreAllMocks());

describe("exact cron admission before GitHub authentication", () => {
  it("admits only the fixed production cron", () => {
    expect(PRODUCTION_CRON).toBe("25 5 * * *");
    expect(() => admitScheduledCron(PRODUCTION_CRON, production)).not.toThrow();
  });

  it("admits an explicitly matching probe cron", () => {
    expect(() => admitScheduledCron("* * * * *", probe)).not.toThrow();
  });

  it.each(["* * * * *", "*/5 * * * *", "", "25  5 * * *", "25 5 * * * "])("rejects production cron %j with zero auth/fetch/PUT", async (cron) => {
    await expectRejected(production, cron, SchedulerErrorReason.UNEXPECTED_CRON);
  });

  it("rejects a different probe cron before auth", async () => {
    await expectRejected(probe, PRODUCTION_CRON, SchedulerErrorReason.UNEXPECTED_CRON);
  });

  it.each([undefined, "", "   "])("rejects missing/empty probe allowed cron %j", async (allowed) => {
    const env = { ...probe };
    if (allowed === undefined) delete env.PROBE_ALLOWED_CRON;
    else env.PROBE_ALLOWED_CRON = allowed;
    await expectRejected(env, "* * * * *", SchedulerErrorReason.INVALID_CONFIGURATION);
  });

  it.each(["PROBE_ALLOWED_CRON", "PROBE_TARGET_REPORT_DATE", "PROBE_REQUEST_ID"] as const)("rejects production with %s present, including an empty value", async (field) => {
    for (const value of ["synthetic", ""]) {
      await expectRejected({ ...production, [field]: value }, PRODUCTION_CRON, SchedulerErrorReason.INVALID_CONFIGURATION);
    }
  });

  it.each(["PROBE_TARGET_REPORT_DATE", "PROBE_REQUEST_ID"] as const)("rejects probe without %s before auth", async (field) => {
    const env = { ...probe };
    delete env[field];
    await expectRejected(env, "* * * * *", SchedulerErrorReason.INVALID_CONFIGURATION);
  });

  it("validates mode before cron and cron before scheduledTime or business identity", async () => {
    await expectRejected({ ...production, DEPLOYMENT_MODE: "unknown" } as unknown as SchedulerEnv, "* * * * *", SchedulerErrorReason.INVALID_CONFIGURATION);
    await expectRejected({ ...probe, PROBE_TARGET_REPORT_DATE: "invalid" }, "unexpected", SchedulerErrorReason.UNEXPECTED_CRON, NaN);
  });
});

async function expectRejected(env: SchedulerEnv, cron: string, reason: SchedulerErrorReason, scheduledTime = SCHEDULED_TIME): Promise<void> {
  const jwt = vi.spyOn(githubApp, "createGitHubAppJwt");
  const fetcher = vi.fn<typeof fetch>();
  const logs: Record<string, unknown>[] = [];
  await expect(runScheduledRecovery({ cron, scheduledTime }, env, {
    fetch: fetcher,
    now: () => NOW,
    sleep: async () => undefined,
    log: entry => { logs.push(entry); },
  })).rejects.toMatchObject({ reason });
  expect(jwt).not.toHaveBeenCalled();
  expect(fetcher).not.toHaveBeenCalled();
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(0);
  expect(logs).toHaveLength(1);
  expect(logs[0]).toMatchObject({
    cron,
    github_auth: { app_jwt_created: "NO", installation_token_created: "NO" },
    control_read: { status: "NOT_STARTED" },
    control_write: { attempted: "NO", result: "NOT_STARTED" },
    result: "FAIL_CLOSED",
    error: { reason },
  });
  expect(logs[0]).not.toHaveProperty("request_id");
  jwt.mockRestore();
}

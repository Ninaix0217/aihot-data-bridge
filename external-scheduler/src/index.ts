import {
  createGitHubAppJwt,
  exchangeInstallationToken,
  normalizePrivateKey,
} from "./github-app";
import { GitHubControlClient } from "./github-control";
import {
  buildExternalTrigger,
  resolveProbeIdentity,
  scheduleContext,
} from "./scheduler";
import {
  type ControlWriteResult,
  type RuntimeDependencies,
  type SchedulerEnv,
  SchedulerError,
  SchedulerErrorReason,
} from "./types";

export async function runScheduledRecovery(
  controller: Pick<ScheduledController, "scheduledTime" | "cron">,
  env: SchedulerEnv,
  dependencies: RuntimeDependencies = runtimeDependencies(),
): Promise<Record<string, unknown>> {
  const started = dependencies.now();
  const context = scheduleContext(
    controller.scheduledTime,
    started,
    resolveProbeIdentity(
      env.DEPLOYMENT_MODE,
      env.PROBE_TARGET_REPORT_DATE,
      env.PROBE_REQUEST_ID,
    ),
  );
  const trigger = buildExternalTrigger(context);
  const observation: Record<string, unknown> = {
    phase: "G1",
    scheduler: "cloudflare",
    cron: controller.cron,
    scheduled_at: context.scheduledAt,
    started_at: context.startedAt,
    schedule_lag_seconds: context.scheduleLagSeconds,
    target_report_date: context.targetReportDate,
    request_id: context.requestId,
    github_auth: {
      app_jwt_created: "NO",
      installation_token_created: "NO",
    },
    control_read: { status: "NOT_STARTED" },
    control_write: { attempted: "NO", result: "NOT_STARTED" },
  };

  try {
    const privateKey = normalizePrivateKey(env.GITHUB_APP_PRIVATE_KEY);
    const auth = observation.github_auth as Record<string, unknown>;
    const jwt = await createGitHubAppJwt(
      env.GITHUB_APP_CLIENT_ID,
      privateKey,
      started,
    );
    auth.app_jwt_created = "YES";
    const installation = await exchangeInstallationToken(
      env.GITHUB_INSTALLATION_ID,
      jwt,
      started,
      dependencies.fetch,
      dependencies.sleep,
    );
    auth.installation_token_created = "YES";

    const client = new GitHubControlClient(
      installation.token,
      dependencies.fetch,
      dependencies.sleep,
    );
    const current = await client.read();
    observation.control_read = {
      status: "PASS",
      previous_blob_sha: current.blobSha,
    };
    const duplicate = current.payload.request_id === trigger.request_id;
    observation.control_write = {
      attempted: duplicate ? "NO" : "YES",
      result: duplicate ? "NOOP_DUPLICATE" : "PENDING",
    };
    const write = await client.update(current, trigger);
    observation.control_write = writeObservation(write);
    observation.result = write.result;
    dependencies.log(observation);
    return observation;
  } catch (error) {
    const safeError = error instanceof SchedulerError
      ? error
      : new SchedulerError(
          SchedulerErrorReason.CONTROL_WRITE_FAILED,
          "unexpected scheduler failure",
        );
    observation.result = "FAIL_CLOSED";
    observation.error = {
      reason: safeError.reason,
      status: safeError.status ?? null,
      message: safeError.message,
    };
    dependencies.log(observation);
    throw safeError;
  }
}

const worker = {
  async scheduled(
    controller: ScheduledController,
    env: SchedulerEnv,
    _ctx: ExecutionContext,
  ): Promise<void> {
    await runScheduledRecovery(controller, env);
  },
} satisfies ExportedHandler<SchedulerEnv>;

export default worker;

function runtimeDependencies(): RuntimeDependencies {
  return {
    fetch: globalThis.fetch,
    now: () => new Date(),
    sleep: (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
    log: (entry) => console.log(JSON.stringify(entry)),
  };
}

function writeObservation(write: ControlWriteResult): Record<string, unknown> {
  if (write.result === "NOOP_DUPLICATE") {
    return {
      attempted: "NO",
      result: write.result,
      previous_blob_sha: write.previousBlobSha,
    };
  }
  return {
    attempted: "YES",
    result: write.result,
    previous_blob_sha: write.previousBlobSha,
    new_blob_sha: write.newBlobSha,
    commit_sha: write.commitSha,
  };
}

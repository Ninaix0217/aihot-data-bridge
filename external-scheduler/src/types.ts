export interface SchedulerEnv {
  GITHUB_APP_CLIENT_ID: string;
  GITHUB_INSTALLATION_ID: string;
  GITHUB_APP_PRIVATE_KEY: string;
  DEPLOYMENT_MODE?: "production" | "probe";
  PROBE_TARGET_REPORT_DATE?: string;
}

export interface RuntimeDependencies {
  fetch: typeof fetch;
  now: () => Date;
  sleep: (milliseconds: number) => Promise<void>;
  log: (entry: Record<string, unknown>) => void;
}

export interface ScheduleContext {
  scheduledAt: string;
  startedAt: string;
  scheduleLagSeconds: number;
  targetReportDate: string;
  requestId: string;
}

export interface ExternalTriggerPayload {
  schema_version: "aihot-external-trigger/v1";
  enabled: true;
  target_report_date: string;
  mode: "RECOVERY";
  request_id: string;
  requested_at: string;
  source: "cloudflare-worker";
}

export interface ControlFile {
  blobSha: string;
  bytes: Uint8Array;
  payload: Record<string, unknown>;
}

export type ControlWriteResult =
  | {
      result: "WRITTEN";
      previousBlobSha: string;
      newBlobSha: string;
      commitSha: string;
    }
  | {
      result: "NOOP_DUPLICATE";
      previousBlobSha: string;
    };

export enum SchedulerErrorReason {
  INVALID_SCHEDULED_TIME = "INVALID_SCHEDULED_TIME",
  INVALID_CONFIGURATION = "INVALID_CONFIGURATION",
  GITHUB_APP_AUTH_FAILED = "GITHUB_APP_AUTH_FAILED",
  GITHUB_API_UNAUTHORIZED = "GITHUB_API_UNAUTHORIZED",
  GITHUB_API_NOT_FOUND = "GITHUB_API_NOT_FOUND",
  GITHUB_API_TRANSIENT_FAILURE = "GITHUB_API_TRANSIENT_FAILURE",
  GITHUB_API_MALFORMED_RESPONSE = "GITHUB_API_MALFORMED_RESPONSE",
  CONTROL_STATE_INVALID = "CONTROL_STATE_INVALID",
  CONTROL_CONCURRENT_UPDATE = "CONTROL_CONCURRENT_UPDATE",
  CONTROL_WRITE_FAILED = "CONTROL_WRITE_FAILED",
}

export class SchedulerError extends Error {
  constructor(
    readonly reason: SchedulerErrorReason,
    message: string,
    readonly status?: number,
  ) {
    super(`${reason}: ${message}`);
    this.name = "SchedulerError";
  }
}

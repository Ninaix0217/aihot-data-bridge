import {
  type ExternalTriggerPayload,
  type ScheduleContext,
  SchedulerError,
  SchedulerErrorReason,
} from "./types";

const BEIJING_TIME_ZONE = "Asia/Shanghai";
const REPORT_DATE = /^\d{4}-\d{2}-\d{2}$/;

export function beijingDateForScheduledTime(scheduledTime: number): string {
  const scheduled = checkedDate(scheduledTime);
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: BEIJING_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(scheduled);
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${value.year}-${value.month}-${value.day}`;
}

export function requestIdForOccurrence(
  targetReportDate: string,
  scheduledTime: number,
): string {
  if (!REPORT_DATE.test(targetReportDate) || !Number.isSafeInteger(scheduledTime)) {
    throw new SchedulerError(
      SchedulerErrorReason.INVALID_SCHEDULED_TIME,
      "cannot derive a deterministic request ID",
    );
  }
  return `${targetReportDate}/cf-${scheduledTime}`;
}

export function scheduleContext(
  scheduledTime: number,
  startedAt: Date,
  probeTargetReportDate?: string,
): ScheduleContext {
  const scheduled = checkedDate(scheduledTime);
  if (!(startedAt instanceof Date) || !Number.isFinite(startedAt.getTime())) {
    throw new SchedulerError(
      SchedulerErrorReason.INVALID_SCHEDULED_TIME,
      "started_at must be a valid instant",
    );
  }
  const targetReportDate = probeTargetReportDate ?? beijingDateForScheduledTime(scheduledTime);
  if (!isCanonicalReportDate(targetReportDate)) {
    throw new SchedulerError(
      SchedulerErrorReason.INVALID_CONFIGURATION,
      "probe target report date must be canonical YYYY-MM-DD",
    );
  }
  return {
    scheduledAt: scheduled.toISOString(),
    startedAt: startedAt.toISOString(),
    scheduleLagSeconds: (startedAt.getTime() - scheduledTime) / 1000,
    targetReportDate,
    requestId: requestIdForOccurrence(targetReportDate, scheduledTime),
  };
}

export function buildExternalTrigger(
  context: ScheduleContext,
): ExternalTriggerPayload {
  return {
    schema_version: "aihot-external-trigger/v1",
    enabled: true,
    target_report_date: context.targetReportDate,
    mode: "RECOVERY",
    request_id: context.requestId,
    requested_at: context.startedAt,
    source: "cloudflare-worker",
  };
}

export function resolveProbeTarget(
  deploymentMode: string | undefined,
  configuredTarget: string | undefined,
): string | undefined {
  const mode = deploymentMode ?? "production";
  if (mode === "production") {
    if (configuredTarget !== undefined && configuredTarget !== "") {
      throw new SchedulerError(
        SchedulerErrorReason.INVALID_CONFIGURATION,
        "production mode forbids a target date override",
      );
    }
    return undefined;
  }
  if (mode !== "probe" || !configuredTarget || !isCanonicalReportDate(configuredTarget)) {
    throw new SchedulerError(
      SchedulerErrorReason.INVALID_CONFIGURATION,
      "probe mode requires an explicit canonical target report date",
    );
  }
  return configuredTarget;
}

function checkedDate(milliseconds: number): Date {
  if (!Number.isSafeInteger(milliseconds)) {
    throw new SchedulerError(
      SchedulerErrorReason.INVALID_SCHEDULED_TIME,
      "scheduledTime must be an integer epoch millisecond value",
    );
  }
  const parsed = new Date(milliseconds);
  if (!Number.isFinite(parsed.getTime())) {
    throw new SchedulerError(
      SchedulerErrorReason.INVALID_SCHEDULED_TIME,
      "scheduledTime is outside the Date range",
    );
  }
  return parsed;
}

function isCanonicalReportDate(value: string): boolean {
  if (!REPORT_DATE.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

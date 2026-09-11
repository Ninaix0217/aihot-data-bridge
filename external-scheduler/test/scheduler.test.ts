import { describe, expect, it } from "vitest";

import {
  beijingDateForScheduledTime,
  buildExternalTrigger,
  requestIdForOccurrence,
  resolveProbeIdentity,
  scheduleContext,
} from "../src/scheduler";
import { SchedulerErrorReason } from "../src/types";
import { NOW, SCHEDULED_TIME } from "./helpers";

describe("logical schedule identity", () => {
  it("derives Beijing D from the nominal scheduledTime", () => {
    expect(beijingDateForScheduledTime(SCHEDULED_TIME)).toBe("2026-09-11");
  });

  it("does not change D when actual execution crosses Beijing midnight", () => {
    const context = scheduleContext(
      SCHEDULED_TIME,
      new Date("2026-09-11T17:30:00Z"),
    );
    expect(context.targetReportDate).toBe("2026-09-11");
    expect(context.scheduledAt).toBe("2026-09-11T05:25:00.000Z");
    expect(context.startedAt).toBe("2026-09-11T17:30:00.000Z");
    expect(context.scheduleLagSeconds).toBe(43_500);
  });

  it("uses deterministic occurrence IDs", () => {
    const first = requestIdForOccurrence("2026-09-11", SCHEDULED_TIME);
    expect(first).toBe(`2026-09-11/cf-${SCHEDULED_TIME}`);
    expect(requestIdForOccurrence("2026-09-11", SCHEDULED_TIME)).toBe(first);
    expect(requestIdForOccurrence("2026-09-11", SCHEDULED_TIME + 60_000)).not.toBe(first);
  });

  it("uses actual start only as requested_at and fixes mode/source", () => {
    const context = scheduleContext(SCHEDULED_TIME, new Date("2026-09-11T08:00:00Z"));
    const payload = buildExternalTrigger(context);
    expect(payload).toEqual({
      schema_version: "aihot-external-trigger/v1",
      enabled: true,
      target_report_date: "2026-09-11",
      mode: "RECOVERY",
      request_id: `2026-09-11/cf-${SCHEDULED_TIME}`,
      requested_at: "2026-09-11T08:00:00.000Z",
      source: "cloudflare-worker",
    });
  });

  it("keeps production D and request ID derived from scheduledTime", () => {
    const identity = resolveProbeIdentity("production", undefined, undefined);
    const context = scheduleContext(SCHEDULED_TIME, NOW, identity);
    expect(identity).toBeUndefined();
    expect(context.targetReportDate).toBe("2026-09-11");
    expect(context.requestId).toBe(`2026-09-11/cf-${SCHEDULED_TIME}`);
  });

  it("fixes probe D and request ID across different nominal occurrences", () => {
    const identity = resolveProbeIdentity(
      "probe",
      "2026-09-10",
      "2026-09-10/cf-probe-123e4567-e89b-12d3-a456-426614174000",
    );
    const first = scheduleContext(SCHEDULED_TIME, NOW, identity);
    const second = scheduleContext(SCHEDULED_TIME + 300_000, NOW, identity);
    expect(first.targetReportDate).toBe("2026-09-10");
    expect(second.targetReportDate).toBe("2026-09-10");
    expect(first.requestId).toBe(identity?.requestId);
    expect(second.requestId).toBe(identity?.requestId);
    expect(first.scheduledAt).not.toBe(second.scheduledAt);
  });

  it("fails closed when probe identity is incomplete or malformed", () => {
    for (const [target, requestId] of [
      [undefined, "2026-09-10/cf-probe-valid"],
      ["2026-09-10", undefined],
      ["2026-09-10", "contains spaces"],
      ["2026-02-30", "2026-09-10/cf-probe-valid"],
    ] as const) {
      expect(() => resolveProbeIdentity("probe", target, requestId)).toThrowError(
        expect.objectContaining({ reason: SchedulerErrorReason.INVALID_CONFIGURATION }),
      );
    }
  });

  it("fails closed when either probe field leaks into production", () => {
    for (const [target, requestId] of [
      ["2026-09-10", undefined],
      [undefined, "2026-09-10/cf-probe-valid"],
      ["", ""],
    ] as const) {
      expect(() => resolveProbeIdentity("production", target, requestId)).toThrowError(
        expect.objectContaining({ reason: SchedulerErrorReason.INVALID_CONFIGURATION }),
      );
    }
  });

  it("rejects unknown deployment modes", () => {
    expect(() =>
      resolveProbeIdentity(
        "other",
        "2026-09-10",
        "2026-09-10/cf-probe-valid",
      ),
    ).toThrowError(
      expect.objectContaining({ reason: SchedulerErrorReason.INVALID_CONFIGURATION }),
    );
  });
});

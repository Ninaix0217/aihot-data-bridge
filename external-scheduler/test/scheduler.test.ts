import { describe, expect, it } from "vitest";

import {
  beijingDateForScheduledTime,
  buildExternalTrigger,
  requestIdForOccurrence,
  resolveProbeTarget,
  scheduleContext,
} from "../src/scheduler";
import { SchedulerErrorReason } from "../src/types";
import { SCHEDULED_TIME } from "./helpers";

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

  it("permits target override only in explicit probe mode", () => {
    expect(resolveProbeTarget("probe", "2026-09-10")).toBe("2026-09-10");
    expect(resolveProbeTarget("production", undefined)).toBeUndefined();
    for (const [mode, target] of [
      ["production", "2026-09-10"],
      ["probe", undefined],
      ["other", "2026-09-10"],
    ] as const) {
      expect(() => resolveProbeTarget(mode, target)).toThrowError(
        expect.objectContaining({ reason: SchedulerErrorReason.INVALID_CONFIGURATION }),
      );
    }
  });
});

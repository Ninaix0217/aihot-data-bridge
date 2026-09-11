import {
  githubRequest,
  isRecord,
  responseJson,
} from "./github-api";
import {
  type ControlFile,
  type ControlWriteResult,
  type ExternalTriggerPayload,
  SchedulerError,
  SchedulerErrorReason,
} from "./types";

export const GITHUB_OWNER = "Ninaix0217";
export const GITHUB_REPOSITORY = "aihot-data-bridge";
export const CONTROL_BRANCH = "aihot-scheduler-control";
export const CONTROL_PATH = ".aihot-control/trigger.json";
const CONTROL_ENDPOINT = `/repos/${GITHUB_OWNER}/${GITHUB_REPOSITORY}/contents/.aihot-control/trigger.json`;
const SHA = /^[0-9a-f]{40}$/;
const REQUEST_ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/;
const SOURCE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const REPORT_DATE = /^\d{4}-\d{2}-\d{2}$/;
const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8", { fatal: true });

export class GitHubControlClient {
  constructor(
    private readonly token: string,
    private readonly fetcher: typeof fetch,
    private readonly sleep: (milliseconds: number) => Promise<void>,
  ) {}

  async read(): Promise<ControlFile> {
    const response = await githubRequest(
      {
        token: this.token,
        method: "GET",
        path: `${CONTROL_ENDPOINT}?ref=${encodeURIComponent(CONTROL_BRANCH)}`,
        expectedStatus: 200,
      },
      this.fetcher,
      this.sleep,
    );
    const value = await responseJson(response);
    if (
      value.type !== "file" ||
      value.encoding !== "base64" ||
      typeof value.sha !== "string" ||
      !SHA.test(value.sha) ||
      typeof value.content !== "string" ||
      typeof value.size !== "number" ||
      !Number.isSafeInteger(value.size) ||
      value.size < 0
    ) {
      malformed("control file response shape is invalid");
    }
    let bytes: Uint8Array;
    try {
      const compact = value.content.replace(/\s/g, "");
      bytes = Uint8Array.from(atob(compact), (character) => character.charCodeAt(0));
    } catch {
      malformed("control file content is not valid base64");
    }
    if (bytes.length !== value.size || (await gitBlobSha(bytes)) !== value.sha) {
      malformed("control file bytes do not match declared size/blob SHA");
    }
    let payload: unknown;
    try {
      payload = JSON.parse(decoder.decode(bytes));
    } catch {
      throw new SchedulerError(
        SchedulerErrorReason.CONTROL_STATE_INVALID,
        "current control file is not valid UTF-8 JSON",
      );
    }
    if (!isRecord(payload)) {
      throw new SchedulerError(
        SchedulerErrorReason.CONTROL_STATE_INVALID,
        "current control payload root is not an object",
      );
    }
    validateCurrentControlPayload(payload);
    return { blobSha: value.sha, bytes, payload };
  }

  async update(
    current: ControlFile,
    payload: ExternalTriggerPayload,
  ): Promise<ControlWriteResult> {
    if (current.payload.request_id === payload.request_id) {
      return { result: "NOOP_DUPLICATE", previousBlobSha: current.blobSha };
    }
    const bytes = encoder.encode(`${JSON.stringify(payload, null, 2)}\n`);
    try {
      return await this.put(current.blobSha, bytes, payload.target_report_date);
    } catch (error) {
      if (
        error instanceof SchedulerError &&
        (error.status === 409 || error.status === 422)
      ) {
        const refreshed = await this.read();
        if (refreshed.payload.request_id === payload.request_id) {
          return { result: "NOOP_DUPLICATE", previousBlobSha: refreshed.blobSha };
        }
        throw new SchedulerError(
          SchedulerErrorReason.CONTROL_CONCURRENT_UPDATE,
          "control file changed to a different request during update",
          error.status,
        );
      }
      throw error;
    }
  }

  private async put(
    blobSha: string,
    bytes: Uint8Array,
    reportDate: string,
  ): Promise<ControlWriteResult> {
    const response = await githubRequest(
      {
        token: this.token,
        method: "PUT",
        path: CONTROL_ENDPOINT,
        expectedStatus: 200,
        body: {
          message: `Trigger AI HOT V2 recovery for ${reportDate} via Cloudflare`,
          content: toBase64(bytes),
          sha: blobSha,
          branch: CONTROL_BRANCH,
        },
      },
      this.fetcher,
      this.sleep,
    );
    const value = await responseJson(response);
    const content = value.content;
    const commit = value.commit;
    if (
      !isRecord(content) ||
      typeof content.sha !== "string" ||
      !SHA.test(content.sha) ||
      !isRecord(commit) ||
      typeof commit.sha !== "string" ||
      !SHA.test(commit.sha)
    ) {
      malformed("control update response shape is invalid");
    }
    return {
      result: "WRITTEN",
      previousBlobSha: blobSha,
      newBlobSha: content.sha,
      commitSha: commit.sha,
    };
  }
}

function validateCurrentControlPayload(payload: Record<string, unknown>): void {
  if (payload.schema_version !== "aihot-external-trigger/v1") {
    invalidState("current control schema is unsupported");
  }
  if (payload.enabled === false) {
    if (
      payload.kind !== "BOOTSTRAP" ||
      Object.keys(payload).sort().join(",") !== "enabled,kind,schema_version"
    ) {
      invalidState("disabled control state must be the exact BOOTSTRAP payload");
    }
    return;
  }
  const expected = [
    "enabled",
    "mode",
    "request_id",
    "requested_at",
    "schema_version",
    "source",
    "target_report_date",
  ].join(",");
  if (
    payload.enabled !== true ||
    Object.keys(payload).sort().join(",") !== expected ||
    payload.mode !== "RECOVERY" ||
    typeof payload.target_report_date !== "string" ||
    !REPORT_DATE.test(payload.target_report_date) ||
    typeof payload.request_id !== "string" ||
    !REQUEST_ID.test(payload.request_id) ||
    typeof payload.requested_at !== "string" ||
    !hasExplicitTimezone(payload.requested_at) ||
    !Number.isFinite(Date.parse(payload.requested_at)) ||
    typeof payload.source !== "string" ||
    !SOURCE.test(payload.source)
  ) {
    invalidState("active control state does not satisfy the external trigger contract");
  }
}

function hasExplicitTimezone(value: string): boolean {
  return /(?:Z|[+-]\d{2}:\d{2})$/i.test(value);
}

async function gitBlobSha(bytes: Uint8Array): Promise<string> {
  const header = encoder.encode(`blob ${bytes.length}\0`);
  const input = new Uint8Array(header.length + bytes.length);
  input.set(header);
  input.set(bytes, header.length);
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-1", input));
  return [...digest].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function toBase64(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function malformed(message: string): never {
  throw new SchedulerError(
    SchedulerErrorReason.GITHUB_API_MALFORMED_RESPONSE,
    message,
  );
}

function invalidState(message: string): never {
  throw new SchedulerError(SchedulerErrorReason.CONTROL_STATE_INVALID, message);
}

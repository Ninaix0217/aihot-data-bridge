# ADR: V2 independent external recovery scheduler

Status: Phase G1 implementation only; not deployed and no recurring cron

## Decision

Use a Cloudflare module Worker as an independent scheduler ingress. ChatGPT
Scheduled Task remains a read/consumer path because its GitHub write capability
has not been proven. GitHub Actions continues to be the V2 compute and
publication plane.

Cloudflare supplies only an explicit report date by updating the Phase F control
file. It does not retrieve AI HOT data, calculate the report window, validate
candidate completeness, compare candidates, publish snapshot-data, dispatch
Actions directly, or modify consumer behavior.

## Identity and retries

The production report date is the Asia/Shanghai calendar date of Cloudflare's
nominal `controller.scheduledTime`. Actual start time affects only
`requested_at` and schedule-lag observability. The request ID is deterministic
for the nominal occurrence. Duplicate delivery is suppressed by reading the
current control file and comparing request IDs; no KV, D1, or Durable Object is
introduced.

GitHub reads and writes use bounded retry only for transport failures, HTTP 429,
and 5xx. Authentication, missing configuration, malformed state, and semantic
failures are not retried. A stale-SHA 409/422 is followed by one read: the same
request becomes a no-op, while a different request fails closed as a concurrent
update.

## Authentication and scope

GitHub App installation authentication is required for real E2E. The App is
installed only on this repository with Contents Read and write. Each invocation
creates an RS256 App JWT using the recommended client ID, then requests a
short-lived installation token narrowed to repository `aihot-data-bridge` and
permission `contents: write`. The private key exists only as a Cloudflare secret.

Repository, branch, path, mode, and source are fixed in code. The Worker can
only update `aihot-scheduler-control/.aihot-control/trigger.json` with mode
`RECOVERY`; it cannot write snapshot-data or choose a workflow/ref.

## Rollout boundary

The committed Wrangler configuration has no cron triggers. A real deployment is
blocked until Cloudflare authentication and GitHub App client ID, installation
ID, and private-key secret are configured. The first App push is expected to
discover the exact GitHub actor and may be rejected by the Phase F allowlist.
Actor approval requires a separate reviewed change. No long-running `05:25 UTC`
cron remains enabled in G1; health-aware recurring recovery belongs to G2.

Cloudflare removes GitHub's native scheduler as the only ingress failure domain,
but it does not protect against a full GitHub Actions outage because Actions
remains the execution plane.

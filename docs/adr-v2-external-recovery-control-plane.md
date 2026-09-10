# ADR: V2 external recovery control plane

Status: implemented for one-time ingress rehearsal; no recurring scheduler or
consumer cutover

## Decision

Introduce a vendor-neutral external trigger ingress that is independent of the
GitHub Actions schedule subsystem. An authorized actor changes exactly
`.aihot-control/trigger.json` on the dedicated
`aihot-scheduler-control` branch. A push-only relay validates that immutable
control event and dispatches the existing V2 repository rehearsal on `main`.

The external scheduler supplies only `target_report_date`, mode `RECOVERY`, and
correlation metadata. It cannot select a repository, workflow, ref, producer
code, report window, retrieval policy, or publication policy. The writer
continues to own Producer(D), source-range proof, VALID_COMPLETE validation,
dominance, non-force CAS, immutable readback, final readback, and V1
preservation.

## Control branch

`aihot-scheduler-control` is an operational control-plane branch, not a code
development branch and must not be merged into `main`. It is initially created
at the exact Phase F main commit. Subsequent operational commits may modify only
`.aihot-control/trigger.json`.

The bootstrap document is disabled and must produce
`IGNORED_BOOTSTRAP`, never a writer dispatch. Active documents use schema
`aihot-external-trigger/v1`, an explicit D, mode `RECOVERY`, a bounded safe
request ID, a timezone-aware request time, and a vendor-neutral source label.
The inclusive replay-age limit is 24 hours. The request time is observability,
not business identity.

## Relay safety boundary

The relay is triggered only by a push to the control branch and path, and then
independently verifies the immutable before/after comparison. A valid event is
exactly one commit whose only changed path is the trigger document. Initial
actor allowlisting contains only repository owner `Ninaix0217`; actor ID is
recorded when available. A different connector actor must fail closed until a
real write probe establishes its identity and an explicit allowlist decision is
made.

The relay has `contents: read` and `actions: write`. It checks out executable
code from `main`, reads the trigger payload from the push event commit SHA, and
may dispatch only `v2-rehearsal.yml` at ref `main`. It cannot write repository
content and does not import producer, upstream, candidate, or repository
publisher code.

## Correlation and duplicates

`request_id` and `trigger_source` are optional observability inputs on the
existing writer. They appear in the run name and summary but never enter the V2
business artifact. Repeated dates or request IDs are allowed to reach the
writer; existing dominance and CAS rules prevent data corruption. No database
or history scan is added solely for trigger deduplication.

An accepted dispatch is not producer success. Evidence remains separated into
relay acceptance, independently correlated writer start/success, and final
repository readback.

## Rollout boundary

Phase E scheduled shadow remains unchanged. Phase F permits one bootstrap and
one manual relay E2E only. It does not create a recurring external scheduler,
scheduled V2 publication, consumer cutover, or ChatGPT Scheduled Task. A later
one-time Scheduled Task write probe must establish the connector's real actor
and full write chain before recurring recovery is considered.

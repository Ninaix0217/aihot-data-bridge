# ADR: V2 scheduled shadow producer

Status: implemented for runtime observation; no consumer cutover

## Decision

V2 is observed under the same two GitHub schedule identities as the V1 daily
producer, but in a separate build-only workflow. The shadow resolves the
scheduled trigger, derives the logical report date, retrieves and validates a
V2 candidate, and reports evidence. It does not publish that candidate.

The shadow schedules are the existing production identities:

- Pass A: `50 4 * * *`
- Pass B: `10 5 * * *`

Both are interpreted by `resolve_scheduled_trigger()`. The workflow does not
calculate a report date from its runtime clock. The resolved logical date is
the only input to the canonical report-window calculation and the existing V2
target-anchored retrieval path.

## Start-time evidence

The preferred actual start is the Actions run API `run_started_at` value. If
that read cannot produce a valid timestamp, the workflow uses a timestamp
captured by its first runner step and labels the evidence
`RUNNER_FALLBACK`. Neither source is used without a timezone.

GitHub does not expose the original occurrence timestamp, so scheduled
identity remains bounded cron inference. A lag above the locked inclusive
18-hour bound fails closed rather than being converted to the runtime date.

## Isolation

The shadow has `contents: read` and `actions: read`, uses concurrency group
`aihot-v2-shadow`, and performs no repository, Pages, consumer, Gmail, or other
external write. The dispatch-only V2 repository rehearsal remains separate
and unchanged. V1 retains its own producer concurrency and publication path.

The shadow retains the normal V2 completeness gate. A primary source that
cannot prove its pagination range makes the shadow run fail; shadow status is
not a reason to weaken source proof.

## Observation gates

One real scheduled Pass A and Pass B for the same Beijing business date are
required before declaring a real schedule pair. Manually dispatched or locally
spoofed events do not satisfy this gate.

Before scheduled V2 repository publication is considered, retain the shadow
for at least three consecutive business dates. Consumer cutover should require
at least seven business dates of overall V2 evidence and a separate connector
acceptance. Neither scheduled publication nor consumer cutover is part of this
decision.

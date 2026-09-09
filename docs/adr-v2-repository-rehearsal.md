# ADR: V2 real repository rehearsal

## Status

Phase D adds a dispatch-only rehearsal path. It does not alter the V1 scheduled
producer or any consumer. Real writes, when explicitly dispatched, are limited
to the `v2/` namespace on the shared `snapshot-data` branch.

## Trigger and isolation

The workflow requires an explicit `target_report_date` and one of `MANUAL`,
`RECOVERY`, or `BACKFILL`. It calls the Phase A workflow-dispatch resolver, so
identity is `EXPLICIT`; there is no default date or runtime inference.

The workflow has no schedule. It uses the existing
`aihot-daily-producer` concurrency group with `cancel-in-progress: false` to
reduce overlap with the V1 producer. Repository CAS remains mandatory because
workflow serialization cannot prove that no other writer exists.

## Namespace

The only mutation paths are:

```text
v2/report-candidate/YYYY-MM-DD.json
v2/latest.json
```

The adapter rejects other branches, non-V2 mutation paths, path traversal, and
force ref updates. V1 `latest.json`, `report-candidate/*`, and unrelated files
must retain their blob identities and bytes.

## Git Data publication

Publication reads the current branch head and immutable state, computes the
Phase C dominance plan, creates a candidate blob and a tree using GitHub's
`base_tree` semantics, then creates a commit whose sole parent is the observed
head. It never reconstructs the repository from a recursive listing. A
recursive listing is used for readback and preservation evidence only; a
`truncated=true` response fails closed.

Before the ref update, the workflow reads back the immutable commit, tree, and
blob and verifies parent, paths, exact bytes, artifact hash, semantic hash,
schema, target date, and completeness. Only then may it perform a non-force ref
update.

After the ref update, verification starts again from
`refs/heads/snapshot-data`. It checks the final dated/latest artifacts, hashes,
tree-diff scope, and V1 path preservation.

## Concurrency and retries

Transport retries are bounded and apply only to network errors and 5xx
responses. CAS retry is a separate three-attempt loop. A 409/422 is treated as
a branch race only when a new ref read proves that the head changed; otherwise
it is an invalid request. After a race, the candidate is compared again against
the new immutable head instead of retrying the old commit or forcing the ref.

## Latest monotonicity

`v2/latest.json` advances by `target_report_date`. A historical backfill may
publish its dated path but cannot rewind latest. Existing latest must have a
byte-identical dated counterpart or repository validation fails closed.

## Consumer boundary

The GitHub Connector, ChatGPT Scheduled Task, Gmail flow, V1 schedules, and
Pages role are unchanged. Successful repository rehearsal is not consumer E2E
and does not authorize a V2 cutover.

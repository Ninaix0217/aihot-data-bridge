# ADR: Disclosed partial candidates and failure-proof observability

## Status

Implemented and tested on isolated branch `codex/v22-proof-diagnostics`, with
remote CI and one real opt-in V2 repository rehearsal verified. Normal integration
does not activate partial mode automatically: new partial writes require opt-in.
V2 rehearsal wrote data; V1, control branch, schedules and consumers are unchanged.
This is not a consumer cutover or a successful Scheduled Task/Gmail E2E.

## Product decision

On 2026-10-05 the user explicitly allowed a clearly marked PARTIAL daily report.
Only items with trustworthy timezone-aware publication timestamps inside
`[D-1 noon, D noon)` Asia/Shanghai may enter its body. Unknown-publication items
must be excluded and the uncertainty disclosed. They must never receive invented
publication times or be assigned to D from collection time.

## Local contract

The existing strict evaluator remains the default. An explicit `allow_partial=True`
or local CLI `--allow-partial` admits a `VALID_PARTIAL` observation only when:

- all existing schema, identity, closure, item-window and time-trust checks pass;
- primary API traversal establishes crossing/exhaustion and passes all existing
  query, observed ordering, page metadata, cursor and page-cap checks;
- the remaining completeness defect is missing/invalid/untrusted upstream
  publication timestamps, disclosed by `invalid_published_at_items`;
- affected API coverage remains `partial`, with source range `INCOMPLETE`.

The source-range proof is not altered. There is no persisted `WINDOW_COMPLETE`
override. Partial is not renamed complete, and unknown-publication counts describe
traversed channel records, not unique unknown items or proven in-window omissions.
HTTP failure, RSS fallback, query/order mismatch, repeated cursor and safety-cap
termination do not become acceptable merely because partial mode is selected.

Existing complete candidate bytes are identical with or without the opt-in.
Repository dominance/publication and the dispatch-only V2 rehearsal support the
same explicit opt-in. The workflow input `allow_partial` is optional and defaults
to false. Existing manual and relay dispatches therefore remain strict. Scheduled
shadow, native schedules, the control branch and formal consumers are unchanged.

## Replacement and readback

Existing trusted PARTIAL candidates can be read without opting in to a new partial
attempt, so a strict later COMPLETE attempt can upgrade them. For the same D and
compatible contract, older attempts remain stale and same-as-of semantic changes
remain conflicts. For later attempts, COMPLETE cannot regress to PARTIAL; PARTIAL
can upgrade to COMPLETE. Within the same quality tier, later observations follow
the existing temporal version rule, without item-count or superset requirements.

Unknown-publication counts enter semantic equality when nonzero; page packing
does not. Existing COMPLETE semantic hashes and artifact bytes are unchanged.
Non-force CAS, immutable/final readback and V1 path preservation remain mandatory.

Rehearsal now distinguishes the attempted artifact from the accepted repository
artifact. KEEP/NOOP verifies and summarizes the existing accepted version rather
than wrongly requiring its bytes to equal the new attempt. Both sets of hashes
remain observable. Repository publication is still rehearsal, not consumer E2E.

## Observability

The shadow CLI captures build observations before validation. On failure it emits
allowlisted proof fields for all primary channels to stderr and Step Summary,
marked `RAW_OBSERVATION_NOT_VALIDATED`. It does not dump item bodies, arbitrary
metadata or raw HTTP responses. Failures before a build return keep their existing
error and do not fabricate proof.

## Live evidence

Read-only reconstruction of D=2026-10-04 used the canonical UTC window
`[2026-10-03T04:00:00Z, 2026-10-04T04:00:00Z)`:

| Channel | Pages | Range | Invalid publication timestamps | In-window records |
| --- | ---: | --- | ---: | ---: |
| selected | 1 | INCOMPLETE, crossed start | 2 | 3 |
| all | 3 | COMPLETE, crossed start | 0 | 150 |
| paper | 1 | COMPLETE, crossed start | 0 | 15 |

Seven logical requests yielded 168 raw in-window records and 150 deduplicated
formal items. Strict validation rejected the observation. Explicit partial mode
created and independently checked a local `VALID_PARTIAL` artifact in about
4.16 seconds; SHA-256:
`fad433b5e9b54ee69e123704232cc6453bc38a97f894e64b3489ce8c00e4e83b`.

A separate bounded six-day `all` traversal used 23 pages and observed 10 invalid
publication timestamps. Both selected records with null timestamps were present
in `all` with null timestamps too; joining channels did not recover their dates.
This is current live evidence, not proof of every historical run's failure cause.

## Remaining integration work

- Temporary branch CI and real V2 repository rehearsal passed: see
  [pinned evidence and one-time consumer test contract](partial-consumer-rehearsal.md).
  This does not enable partial consumer behavior by itself.
- Version/identify the consumer acceptance policy before enabling PARTIAL
  consumption; existing consumers may correctly reject these artifacts.
- Decide the actual daily readiness deadline and report freeze/version policy.
- Review direct Cloudflare workflow dispatch versus the control-branch relay;
  no App permission, credential, control branch or scheduler change is made here.
- `retrieval.as_of` is traversal completion time, not evidence of an atomic
  upstream snapshot. Late arrivals and cursor snapshot isolation remain separate
  limits. The <=18h native-cron inference is not proof against multi-day aliasing.

No formal consumer, Gmail, Pages or scheduler is modified by this local change.

# ADR: V2 candidate versioning and safe repository foundation

## Status

Phase C is implemented locally and is not deployed. It performs no GitHub
write, creates no scheduled shadow producer, and changes no V1 consumer path.

## Candidate semantics

A V2 candidate is a temporal observation of one `target_report_date`: it
describes the fixed report window and data visible to AI HOT at
`retrieval.as_of`, with source completeness proven under a named producer,
query, and source-range contract.

Completeness is a qualification gate. Item membership is not monotonic. A
later complete observation may legitimately add, remove, merge, or correct an
item, so item counts and set inclusion are not dominance scores.

## Contract compatibility

Candidates are comparable only when schema version, producer contract,
target date, canonical report window, upstream window, ordering, primary query
identities, and source-range evaluator version match. Runtime workflow IDs and
Git commit SHAs are not part of the business artifact.

## Dominance

After both candidates are valid and contract-compatible,
`retrieval.as_of` orders observations:

- an older attempt is kept out;
- equal `as_of` and equal semantic content is an idempotent no-op;
- equal `as_of` with different semantic content is a conflict;
- a later complete observation replaces the older one, including when it has
  fewer items;
- later observations with the same semantic content are explicitly classified
  as equivalent but fresher.

Malformed existing repository state is a conflict and is never silently
repaired by ordinary publication.

## Hashes

`CONTENT_HASH` covers business contract, stable channel qualification, summary,
and normalized formal items. It excludes `generated_at`, `retrieval.as_of`, and
operational pagination packing such as page counts and oldest fetched item.

`ARTIFACT_SHA256` covers the complete deterministic serialized JSON bytes.
Thus a later equivalent observation can retain the content hash while changing
the artifact hash.

## Repository namespace and latest

V2 may write only:

```text
v2/report-candidate/YYYY-MM-DD.json
v2/latest.json
```

The dated path is derived from validated `target_report_date`; callers cannot
supply an arbitrary path. `v2/latest.json` advances by target date, never by
generation, retrieval, or commit time. Historical backfill may update its
dated path but cannot rewind latest. Any existing latest object must have a
byte-identical matching dated candidate; missing or divergent state fails
closed instead of being repaired implicitly.

## Safe publication contract

The Phase C publisher is expressed against a Git object adapter and is tested
with a deterministic in-memory object/ref store. A future GitHub adapter must:

1. read branch head and its immutable commit/tree;
2. validate existing dated/latest candidates and compute a dominance plan;
3. create only the required candidate blob and a tree based on the current
   head, preserving every unrelated V1 and V2 path;
4. read back blob, tree, commit parent, paths, bytes, and hashes before updating
   the branch ref;
5. update `snapshot-data` non-force;
6. read back the final ref and final path bytes, including byte-identical dated
   and latest files when both are updated.

If the head changes, the attempt is discarded. The publisher rereads the new
head and recomputes dominance rather than retrying the old commit. Three
consecutive races fail closed as `REPOSITORY_CONCURRENT_UPDATE`.

## Limits

No GitHub REST adapter or real `snapshot-data` write is activated in Phase C.
Live parallel repository rehearsal and workflow integration belong to a later
phase.

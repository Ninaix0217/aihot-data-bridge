# ADR: Target-anchored retrieval and source completeness

## Status

Phase B implemented locally. It is not connected to the production workflow,
repository data branch, Pages, GitHub Connector, or Scheduled Task.

## Decision

V2 retrieval is anchored to an explicit `target_report_date`. The canonical
`report_window_for_date()` function derives the fixed business window:

```text
[D-1 12:00:00, D 12:00:00) Asia/Shanghai
```

Runtime, workflow start time, and `generated_at` do not alter that window.

## Upstream horizon and local filtering

The three primary channels (`selected`, `all`, and `paper`) request AI HOT's
supported `window=7d`, `by=published`, `limit=100` API. Retrieval can inspect
the seven-day source horizon, while the V2 artifact retains only primary items
whose trustworthy `published_at` is inside the fixed 24-hour business window.

The initial V2 page cap is 30 and is independent from V1's 10-page cap. A cap
is only a safety bound. Reaching it without proof is incomplete; reaching it
on the same page that establishes proof is complete.

## Completeness proof

Each primary channel traverses its cursor sequentially until either:

- a page contains a valid `publishedAt` strictly earlier than `report_start`;
  or
- `nextCursor` is exhausted.

Equality with `report_start` is not sufficient while another cursor exists,
because the next page may contain more records at the inclusive boundary.

Formal completeness additionally requires a verified API schema/query echo,
`publishedAtDesc` metadata, observed newest-to-oldest ordering, consistent page
metadata, no missing/malformed/naive `publishedAt`, no repeated cursor, and no
page-cap termination before range proof. One shared evaluator derives stored
source-range state and candidate completeness from this evidence.

RSS may retain useful diagnostic data when the API fails, but it has no cursor,
total count, or equivalent range proof. It therefore cannot establish formal
V2 completeness.

## Supplementary sources

`hot_topics` is point-in-time and `dailies/latest` is not a historical API.
Their source-range state is `NOT_APPLICABLE`, and they do not control primary
report-window completeness. For a historical target, current supplementary
responses are not fetched or represented as historical data.

## Trust and empty results

A complete candidate requires a canonical report window, timezone-aware
generation/retrieval timestamps, `generated_at >= retrieval.as_of`, a closed
report window, complete proof for all primary channels, and only in-window
formal items. An empty artifact is valid when all three primary APIs prove an
empty exhausted range. This is distinct from an empty result caused by failed
or incomplete retrieval.

## Backfill admission

Phase B permits an attempted retrieval only when the target is no more than
four report dates behind the latest closed Beijing report date and
`report_start >= retrieval.as_of - 6 days`. Admission does not imply source
completeness; pagination must still prove the actual range. Repository backfill
publication is outside Phase B.

## Late arrivals

`VALID_COMPLETE` means range-complete relative to data visible from AI HOT at
`retrieval.as_of`. It does not prove that no item with an in-window publication
time will be indexed by AI HOT later.

## Production isolation

V1 `BridgeService.today()`, rolling 30-hour retrieval, schema, snapshot paths,
validation, publication, workflow, and consumers are unchanged. The V2 CLI
writes only an explicitly requested local file and refuses to write an artifact
that does not pass the V2 completeness evaluator.

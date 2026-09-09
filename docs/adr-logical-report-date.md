# ADR: Logical report date foundation

## Status

Phase A implemented locally; not connected to the production producer.

## Decision

Producer business identity becomes logical-date-driven. The business key is
`target_report_date`, not workflow start time or snapshot generation time.

For report date `D`, the only report window is:

```text
[D-1 12:00:00, D 12:00:00) Asia/Shanghai
```

The window is calculated with `ZoneInfo("Asia/Shanghai")` by the shared
`report_window_for_date()` primitive.

Scheduled events identify Pass A and Pass B only from the exact
`github.event.schedule` value. Their nominal daily UTC occurrence is inferred
from `started_at`. The inference is accepted only when the inclusive lag is
between zero and 18 hours. Its status is `BOUNDED_CRON_INFERENCE`, never
`VERIFIED`. Unknown schedules and runs outside the bound fail closed.

Future `workflow_dispatch` integration must require an explicit
`target_report_date` and an explicit `MANUAL`, `RECOVERY`, or `BACKFILL` mode.
It must not default the target to the current date. Its identity status is
`EXPLICIT`.

`generated_at` is not an input to logical identity resolution. It remains the
time at which a future candidate artifact finishes generating.

## Scope

Phase A provides immutable domain types, parsers, validation, and regression
tests only. The production workflow does not call this foundation yet. V1
rolling retrieval, snapshot paths, validation, repository publication, Pages,
and consumer behavior remain unchanged.

## Limit

GitHub does not expose a trustworthy original occurrence timestamp for a
scheduled event. A cron occurrence derived from `started_at` is therefore a
bounded inference. A delay beyond the accepted bound is unresolved; a delay
that crosses a later daily occurrence cannot be proven from these inputs alone.

## Future

A future external scheduler should pass an explicit report date and reduce the
system's dependence on inferred schedule identity. Target-anchored retrieval,
schema V2, backfill fetching, dominance, and production workflow integration
belong to later phases.

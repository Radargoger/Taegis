# Policies and metrics

## Source views

The SOCRadar incidents API filters by `status` with exactly one value:
`OPEN`, `ON_HOLD`, or `CLOSED`. The bridge reads all three views.
Unresolved = `OPEN` + `ON_HOLD`. `ON_HOLD` records report the granular
status `INVESTIGATING` and map to an `ACTIVE` Case. `CLOSED` records
report `RESOLVED`, `MITIGATED`, or `FALSE_POSITIVE`.

Every request carries an explicit epoch-second `start_date`/`end_date`.
A dateless request silently gets a ~30-day server-side window, so a full
scan always sends `start_date=0`.

## Duplicate policy (P1)

An alarm seen in both an unresolved view and the closed view is a normal
status transition. The closed view wins and an audit event is recorded.
The same alarm in both `OPEN` and `ON_HOLD` is data corruption: the run
fails closed with zero mutations. A repeated key inside one view also
fails closed.

## Failure policy (P2)

A mutation failure is isolated to its alarm. Later alarms still process.
A run with any failed alarm ends failed, never clean. A snapshot-level
failure (source or target read incomplete) means zero mutations.
Mutations are never retried automatically; reads are.

## Ownership

A Case belongs to the bridge when its tags contain exactly one
`socradar-company-<company_id>-alarm-<alarm_id>` tag. The Taegis
platform rewrites `serviceDeskType` and truncates `serviceDeskId` after
create, so those fields carry no ownership. The canonical alarm URL is
in the `keyFindings` `Source:` line. Zero or multiple candidate tags,
or two Cases with the same tag, fail closed for operator review.

## Scheduling

| Strategy | Behavior |
|---|---|
| `layered` (default) | Three tiers driven by a persisted marker. |
| `full` | Every run scans `start_date=0` to now. Lifecycle-complete fallback. |

Layered tiers:

| Tier | When | Window |
|---|---|---|
| initial | first run ever | initial lookback days |
| incremental | every run after that | poll interval + overlap minutes |
| reconcile | first run at/after the reconcile hour, once per day | reconcile lookback days |

The SOCRadar date filter is creation-based (live-verified): updating an
old alarm does not move it into a recent window. Under `layered`, a late
change to an alarm created before the reconcile window can be missed.
That is the accepted default trade-off; switch to `full` when that gap
is unacceptable. A tier marker
only advances after a successful run, so a failed window is retried.

## Weekly metrics

The two solutions expose different weekly numbers. They are not the
same thing.

| Metric | Where | Meaning |
|---|---|---|
| `alarms_occurred_last_7_days` | Azure run summary | alarms whose SOCRadar occurrence date falls in the last 7 days |
| `events_last_7_days` | VPS health/metrics | successful bridge Case mutations in the last 7 days, by bridge write time |

## Health semantics (VPS)

`/healthz` always returns HTTP 200 while the process is alive. The body
`status` field is `ok` only when the last run succeeded; `partial_failure`,
`failed`, `running`, and no-run-yet all report `degraded`.

## HTTP Ingest (VPS, optional)

Sends a small normalized lifecycle event after a successful Case
mutation. Best effort, no durable outbox, not at-least-once. It is not
the Case lifecycle source of truth.

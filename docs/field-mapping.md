# Field and status mapping

## Source status vocabulary (live-verified 2026-07-21)

The SOCRadar incidents/v4 list endpoint filters with a single-valued `status`
parameter: `OPEN`, `ON_HOLD`, or `CLOSED`. A comma-separated status list
returns HTTP 500, and the legacy `is_resolved` parameter is ignored by the
live API. Record-level `status` values per list view:

| List view | Record `status` values | Side | Taegis Case status |
|---|---|---|---|
| `OPEN` | `OPEN` | unresolved | `OPEN` |
| `ON_HOLD` | `INVESTIGATING` | unresolved | `ACTIVE` |
| `CLOSED` | `RESOLVED`, `FALSE_POSITIVE`, `MITIGATED` | resolved | close mapping below |

## Field mapping

| SOCRadar field | Taegis Case field | Rule |
|---|---|---|
| `company_id` + `alarm_id` | tag | `socradar-company-<company_id>-alarm-<alarm_id>`; exactly one per Case; the sole ownership authority |
| `alarm_id` + `company_id` | `keyFindings` `Source:` line | canonical SOCRadar alarm URL, ending in `?id=<id>`; analyst link, never an ownership signal |
| `alarm_generic_title` | `title` | `[SOCRadar] #<id> - <title>`, max 256 chars |
| `alarm_risk_level` | `priority` | LOW=1, MEDIUM=2, HIGH=3, CRITICAL=4 |
| status/type/text/date/link | `keyFindings` | bounded Markdown/plain-text snapshot |
| OPEN | `status` | `OPEN` |
| INVESTIGATING | `status` | `ACTIVE` |
| RESOLVED | close status | `CLOSED_CONFIRMED_SECURITY_INCIDENT` |
| MITIGATED | close status | `CLOSED_THREAT_MITIGATED` |
| FALSE_POSITIVE | close status | `CLOSED_FALSE_POSITIVE_ALERT` |
| unknown closed status | close status | `CLOSED_INCONCLUSIVE` |

## Why `serviceDeskId`/`serviceDeskType` are not mapped

The `serviceDesk` fields are absent from create/update inputs and from every
ownership check. Live verification (2026-07-21, Taegis US1, two independent
Cases): the create response echoes the submitted values, then the platform
asynchronously coerces `serviceDeskType` to `SNOW` and truncates
`serviceDeskId` to 40 characters within minutes. A platform-rewritten field
can carry neither ownership nor a reliable analyst link; the canonical alarm
URL therefore lives in the `keyFindings` `Source:` line.

Close mappings are configuration, not immutable business rules. The customer may
prefer `CLOSED_INFORMATIONAL` or `CLOSED_INCONCLUSIVE` for `RESOLVED`; that choice
is an open business decision and must be confirmed before production apply
(see `open-questions-and-live-gates.md`).

The bridge creates Cases without Taegis alert evidence. Therefore
`alertsResolutionStatus` is omitted from `closeInvestigation`; live
verification (2026-07-21) confirmed Taegis accepts the close without it when
the Case has no attached alerts.

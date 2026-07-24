# SOCRadar to Secureworks Taegis - Azure

Syncs SOCRadar alarms into Secureworks Taegis Investigation Cases.
[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Forcunsami%2FSOCRadar-Taegis-Azure%2Fmaster%2Fazuredeploy.json)

Runs as an Azure Consumption Logic App. One alarm becomes one Case.
When the alarm changes, the Case is updated. When the alarm closes,
the Case is closed. A closed Case is never reopened.

## Requirements

- Azure subscription with a resource group.
- Azure Key Vault with two secrets: `socradar-api-key`, `taegis-client-secret`.
- SOCRadar company ID and API key.
- Taegis tenant ID, client ID, client secret (role must manage Investigations).
- Azure CLI logged in.

## Taegis regions

| Region | Base URL |
|---|---|
| US1 | https://api.ctpx.secureworks.com |
| US2 | https://api.delta.taegis.secureworks.com |
| US3 | https://api.foxtrot.taegis.secureworks.com |
| EU1 | https://api.echo.taegis.secureworks.com |
| EU2 | https://api.golf.taegis.secureworks.com |

## Deploy from the Azure portal (no CLI)

1. Click the "Deploy to Azure" button above. The portal opens the
   parameter form. (Alternative: portal > "Deploy a custom template" >
   paste `azuredeploy.json`.)
2. Pick a resource group. Fill the form: company ID, tenant ID, client ID,
   region base URL. Leave `ledgerStorageAccountName` empty to auto-generate a
   globally unique name.
3. Paste the SOCRadar API key and Taegis client secret directly into the
   two secure fields. Nothing is stored in the template. The workflow
   strips surrounding whitespace and invisible paste artifacts from
   credentials, but still paste carefully.
4. Deploy. The first run self-starts about 3 minutes later and starts
   writing Cases. For a read-only trial set `syncMode` = `audit` first.

## Setup (CLI)

1. Put the two secrets into your Key Vault. Enable template deployment access.
2. Copy `parameters.example.json` to `parameters.local.json`. Do not commit it.
3. Fill in company ID, tenant ID, client ID, region URL, Key Vault resource ID,
   a globally unique storage account name, and optionally a Log Analytics
   workspace resource ID for alarm-level reporting.
4. Validate, then deploy:

```bash
sh scripts/deploy.sh <resource-group> parameters.local.json          # what-if
sh scripts/deploy.sh <resource-group> parameters.local.json --apply  # deploy
```

The deployment creates the Logic App, a storage ledger (managed identity,
no shared keys), and optional Log Analytics ingestion.

## First run

The template deploys production-ready and enabled: the first run starts
on its own about 3 minutes after deployment, then every
`recurrenceIntervalMinutes`. It creates a Case for every open alarm in
the initial window and keeps them in sync from then on. A built-in Log
Analytics workspace (`<workflowName>-law`) receives alarm-level events;
query `SOCRadarTaegisBridge_CL` with `kql/queries.kql`.

Prefer a dry run first? Deploy with `syncMode=audit`: everything is read
and planned, nothing is written until you redeploy with `apply`.

## Controlled rollout (optional)

The default is apply-all. For a staged rollout instead:

1. Deploy with `syncMode=audit` and review the planned actions.
2. Redeploy with `applyAlarmIds` set to one disposable test alarm ID; a
   non-empty allowlist overrides apply-all, so only that alarm is
   written. Verify the Case in Taegis.
3. Redeploy with an empty `applyAlarmIds` to return to apply-all.

`maxMutationsPerRun` caps Taegis writes per run (default 100).

## Scheduling

`scanStrategy=layered` (default) uses three tiers driven by a state
marker:

| Tier | When | Window |
|---|---|---|
| initial | first run ever | last `initialLookbackDays` days (default 1) |
| incremental | every run | interval + `overlapMinutes` (default 15 + 10 = last 25 minutes) |
| reconcile | daily, first run at/after `reconcileHourUtc` (default 06:00 UTC) | last `reconcileLookbackDays` days (default 7) |

The reconcile tier re-pulls recent alarms so a status or severity change
made in SOCRadar is reflected in the Case.
Every run records which tier it was in its `window_mode` field, in the
run summary, the ledger, and the `WindowMode` column in Log Analytics.
So "did the daily reconcile run today" is one query.
The SOCRadar date filter is creation-based, so `layered` can miss a late
change to an alarm older than `reconcileLookbackDays`. Switch to `full` (rescans the entire
history, `start_date=0`, every run) when completeness matters more than
run time; at high alarm volume `full` runs get long. Details: `docs/policies-and-metrics.md`.

## Reporting

If `logAnalyticsWorkspaceResourceId` is set, every run writes alarm-level
events to the `SOCRadarTaegisBridge_CL` table. Ready-made queries are in
`kql/queries.kql`: how many alarms were seen, created, updated, closed,
failed, drifted.

## State

The storage account holds two tables. `SocradarTaegisMapping` maps each
alarm to its Case and stores the sync fingerprint. `SocradarTaegisEvents`
stores drift events and one summary row per run. The ledger is bound to
one tenant and company pair and the workflow stops if they do not match.
Tables have no TTL; clean up per `ledgerRetentionDays` manually.

## Reopen policy

A closed Case is never reopened automatically by default. If the source
alarm goes back to an open state in SOCRadar, the run records a
`drift_after_close` event and leaves the Case closed for the analyst.

Set `reopenPolicy` to `auto` to change that: the workflow then reopens
the Case with the open status and refreshed content, and records
`case_reopened`. The mutation cap and per-alarm isolation apply as
usual. Keep `manual` unless your workflow wants SOCRadar to win over
analyst-closed Cases.

## Audit trail

Every action is recorded in three places:

- `SocradarTaegisEvents` table: one summary row per run and one row per
  drift event.
- `SocradarTaegisMapping` table: one row per alarm with its Case ID,
  statuses, and last success time.
- `SOCRadarTaegisBridge_CL` in Log Analytics (if configured): one row per
  alarm-level action. Example:

```kusto
SOCRadarTaegisBridge_CL
| where TimeGenerated > ago(24h)
| summarize count() by EventType
```

The Logic App run history additionally shows every run with its full
event list in the `Compose_Run_Summary` output.

## Rules

- Never run two writers for the same SOCRadar company and Taegis tenant.
  This includes the separate VPS solution.
- Do not create Taegis Cases with SOCRadar tags by hand. The bridge
  fails closed on ownership conflicts.
- Case ownership is the `socradar-company-<id>-alarm-<id>` tag. The
  serviceDesk fields are not used; the platform rewrites them.

## Validation

```bash
sh scripts/validate.sh        # bicep build + JSON checks
python3 -m pytest tests -q    # workflow contract tests
```

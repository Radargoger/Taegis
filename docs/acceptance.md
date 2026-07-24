# Live acceptance runbook

Run this once per customer environment. Every step has a visible check.

1. Deploy with defaults into a fresh resource group. Expected: the first
   run self-starts in about 3 minutes and succeeds.
2. Compare `source_total` in the run summary with the SOCRadar API total
   for the same window.
3. Pick one open alarm in SOCRadar and find its Case in Taegis
   (`Search Case by SOCRadar alarm tag` in the Postman collection).
4. Change the alarm severity or title in SOCRadar. After the next run,
   the Case shows the updated content.
5. Close the alarm in SOCRadar. After the next run, the Case is closed
   with the mapped close status.
6. Change the closed alarm again. After the next run, the Case stays
   closed and the ledger records a `drift_after_close` event
   (`SocradarTaegisEvents` table).
7. Optional: set `reopenPolicy=auto`, reopen the alarm in SOCRadar, and
   watch the Case return to an open status.
8. Check `SOCRadarTaegisBridge_CL` in the built-in Log Analytics
   workspace with `kql/queries.kql` (ingest can lag 5-10 minutes).
9. Cleanup for test tenants: strip the SOCRadar tags from test Cases
   (update, then archive; archiving alone keeps them visible to the tag
   search) and delete the resource group.

Never run two writers (this Logic App and the VPS service, or two Logic
Apps) for the same SOCRadar company and Taegis tenant.

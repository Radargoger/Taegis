# Security policy

This repository must never contain live SOCRadar API keys, Taegis client secrets,
Taegis HTTP Ingest keys, bearer tokens, raw customer alarms, or production tenant
identifiers.

## Credential handling

- Keep VPS credentials in a root-readable environment file or a secret manager.
- Supply Azure secure parameters from Key Vault references. Do not put secret
  values in parameter files or deployment commands saved to shell history.
- Postman environments in Git contain variable names only. Use Postman's local
  `current value` for secrets and do not export it.
- Logic App HTTP actions that carry credentials or tokens use secure input/output
  settings.
- Logs redact `API-Key`, `Authorization`, client secrets, access tokens, and HTTP
  Ingest keys.

## Reporting

If a credential is committed, revoke or rotate it first. Removing a value from the
latest commit does not remove it from Git history.


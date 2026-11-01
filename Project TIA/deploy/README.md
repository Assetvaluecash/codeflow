# Deploying TIA

**Status: written and statically checked only.** There was no Docker or Kubernetes on the development machine: the image has never been built, the Compose file
never started and the Helm chart never installed or rendered by `helm`. `tests/test_deploy.py` checks what can be checked without them (valid YAML, every secret
from a file, non-root, read-only file systems, dropped capabilities, health probes on the real endpoints, migration before the server, settings that exist in the code).
Treat the first real build and install as a verification step.

| File | For |
|---|---|
| `Dockerfile` | one image; `python -m <command>`: `tia.server` (default), `tia.worker`, `tia.migrate`, `tia.tenants` |
| `docker-compose.yml` | a single machine: PostgreSQL 16, the migration (once), the API and a worker |
| `helm/tia` | Kubernetes: API (2 replicas, PDB), workers (4), the migration as a pre-install/pre-upgrade hook, a shared workspace volume kept on uninstall, optional ingress, network policy |

## Secrets

Nothing secret is in these files. Compose reads them from `deploy/secrets/<name>`; the chart from a Kubernetes Secret (`existingSecret`, default `tia-secrets`) with keys:
`app-dsn`, `worker-dsn`, `admin-dsn`, `app-password`, `worker-password`, `audit-key`, `master-key`, `metrics-token` (and, when used, `github-webhook-secret`,
`github-app-private-key`, `ado-hook-password`, `ado-token`; point the matching `TIA_*_FILE` variables at them through `extraEnv`).
`master-key` is 32 random bytes in base64; DSNs must use `sslmode=verify-full` off-machine.

## First start

1. Build: `docker build -f deploy/Dockerfile -t tia:0.1.0 .` (from `Project TIA/`).
2. Create the secrets, start the database and run the migration (Compose does it before the API starts; the chart's hook does it before any pod).
3. Create the first tenant and its identity provider with the admin DSN: `docker compose run --rm migrate tia.tenants create acme-bank`, then `tia.tenants idp-set ...`
   (see [the administrator guide](../docs/ADMIN-GUIDE.md)). Put the tenant id in `TIA_TENANT_ID` / `defaultTenantId`.
4. Put a TLS-terminating reverse proxy with rate limiting in front of port 8080 (Compose binds it to 127.0.0.1 only).

The workspace volume holds clones (re-fetchable) and **uploaded code (not re-fetchable)**: back it up with the database.

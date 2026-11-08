# Deploying TIA

**Status (2026-11-06/08):**
- **Image:** built with Docker Desktop 29.9 (480 MB, non-root, read-only file system).
- **Compose:** run end to end on one machine:
  - migration of all 17 schema versions;
  - tenant creation with `tia.tenants`;
  - API and worker ready (`/readyz`: database, schema, workspace, worker heartbeat);
  - an authenticated service token uploading 6 repositories and analysing a breaking change (5 findings);
  - the audit chain verified;
  - the migration re-run on the live system (the upgrade path: nothing to apply);
  - an encrypted tenant backup restored into a second database and verified (26 tables, no mismatch).
- **Helm chart:** installed on Kubernetes 1.36 (Docker Desktop, kubeadm, one node) on 2026-11-08:
  - the migration ran as the pre-install hook; 2 API and 2 worker replicas became ready;
  - a tenant was created from a pod;
  - `helm upgrade` (revision 2) re-ran the migration hook and rolled the pods;
  - through the service: `/readyz` ready, an authenticated upload of 6 repositories and an analysis (5 findings), audit chain verified;
  - an API pod and a worker pod were deleted: health checks kept answering and both pods were replaced.
  - Fixed by this run: the migration job ignored `extraEnv` and had no `imagePullPolicy`.
  - **Not verified:** a multi-node cluster, node failure, a ReadWriteMany workspace (the single node used ReadWriteOnce), an ingress, the network policy under a CNI that enforces it.
- **Azure Terraform** (`deploy/terraform/azure`): **never validated or applied**.

`tests/test_deploy.py` also checks statically: valid YAML, every secret from a file, non-root, read-only file systems, dropped capabilities, health probes on
the real endpoints, migration before the server, settings that exist in the code.

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

1. Build: `docker build -f deploy/Dockerfile -t tia:1.0.0 .` (from `Project TIA/`).
2. Create the secrets, start the database and run the migration (Compose does it before the API starts; the chart's hook does it before any pod).
3. Create the first tenant and its identity provider with the admin DSN: `docker compose run --rm migrate tia.tenants create acme-bank`, then `tia.tenants idp-set ...`
   (see [the administrator guide](../docs/ADMIN-GUIDE.md)). Put the tenant id in `TIA_TENANT_ID` / `defaultTenantId`.
4. Put a TLS-terminating reverse proxy in front of port 8080 (Compose binds it to 127.0.0.1 only). TIA rate-limits itself too (`tia/ratelimit.py`).
5. Compose keeps PostgreSQL on an internal-only network and sets `TIA_ALLOW_INSECURE_DB` for that hop; a database on another machine needs TLS (`sslmode=verify-full`). The backup key is the `backup_key` secret (`TIA_BACKUP_KEY_FILE`).

The workspace volume holds clones (re-fetchable) and **uploaded code (not re-fetchable)**: back it up with the database.

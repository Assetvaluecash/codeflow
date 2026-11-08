# TIA operations: high availability, backup and disaster recovery

Written for the platform team that runs TIA (SaaS operations or a bank's own infrastructure team). PRD references: PL-004 (HA), PL-005 (backup/DR),
PL-010 (air-gapped), section 11 targets (99.9% monthly availability, RPO ≤ 1 hour, RTO ≤ 4 hours, both *proposed*).

**Verification status.** The application behaviours below (stateless servers, leased jobs, reaping, encrypted tenant backup and restore,
retention anchors) are covered by automated tests against a real PostgreSQL. The RPO/RTO figures depend on the database and infrastructure
the operator provides and have **not** been measured in a production-like environment; they are design targets.

## 1. What holds state

| Component | State | Loss means |
|---|---|---|
| PostgreSQL | everything that matters: tenants, configuration, reports, evidence snapshots, evidence, audit chain, job queue, encrypted secrets | data loss: protect with PITR (section 3) |
| Workspace volume (`TIA_WORKSPACE`) | bare clones (re-fetchable) and **uploaded code (not re-fetchable)** | clones: none (re-fetched); uploads: must be backed up |
| Key-encryption key (`TIA_MASTER_KEY`, Vault Transit, or the tenant's KMS key) | outside the database by design | **all tenant secrets unreadable**: back it up separately, never with the database |
| API and worker processes | none (stateless) | nothing: restart or reschedule |

## 2. High availability

* **API**: 2+ replicas behind a load balancer (`api.replicas`, default 2) with a PodDisruptionBudget (`podDisruptionBudget.minAvailable`). Any replica
  serves any tenant; sessions are in the database.
* **Workers**: 2+ replicas (`worker.replicas`, default 4). Jobs are claimed with `for update skip locked` and a **lease**; a worker heartbeats while it
  works. If a worker dies, `reap_expired` re-queues its job when the lease ends (at-least-once; analyses are idempotent by key, so a repeat produces
  the same report). Per-tenant concurrency caps hold across all workers (advisory lock).
* **Database**: a managed PostgreSQL with a synchronous or near-synchronous standby and automatic failover (Azure Database for PostgreSQL
  Flexible Server zone-redundant HA, Amazon RDS Multi-AZ, Cloud SQL HA, or Patroni). TIA reconnects through its pool after a failover; requests in
  flight during the switchover fail and are retried by the client (jobs by the queue).
* **Probes**: `/healthz` (liveness) and `/readyz` (database, schema version, workspace, worker heartbeats) drive restarts and traffic.
* **Workspace**: a ReadWriteMany volume (Azure Files, EFS, Filestore) shared by API and workers, or per-pod volumes if uploads are disabled
  (`TIA_ALLOW_UPLOADS=false`: clones are re-fetched on demand).

## 3. Backup

| Layer | How | RPO it gives |
|---|---|---|
| Database (platform) | continuous WAL archiving + daily base backup (the managed service's PITR, or pgBackRest/WAL-G), 35-day window | minutes (WAL shipping interval), ≤ 1 hour target |
| Database (cross-region) | geo-redundant backup storage or a cross-region read replica | as above, in the recovery region |
| Tenant export | `python -m tia.backup export <tenant> --out x.tiabak`: every tenant-owned table, AES-256-GCM under `TIA_BACKUP_KEY`, digests per table | the export's age (for tenant moves, legal hold, offboarding copies) |
| Workspace uploads | volume snapshots (hourly) | ≤ 1 hour |
| Keys | KEK in the KMS/Vault's own backup; `TIA_BACKUP_KEY` in the secret manager | n/a |

A tenant export is bound to its tenant id (it is the associated data of the encryption), refuses to restore over an existing tenant, and refuses a
database at another schema version. `python -m tia.backup verify` compares row counts and digests after a restore; `GET /api/v1/audit/verify`
then checks the audit chain.

## 4. Disaster recovery runbook (region or database loss)

1. Declare the incident; freeze deployments.
2. Restore the database in the recovery region to the latest point (PITR restore, or promote the cross-region replica).
3. Restore the key-encryption key access (Vault/KMS in that region) and set `TIA_REGION` to the recovery region **only if** the tenants' residency allows
   it (a tenant pinned to a region is refused elsewhere: `421 wrong_region`).
4. Deploy the same TIA version with Helm; run `python -m tia.migrate` (a no-op when the schema is current).
5. Check `/readyz`, then `GET /api/v1/audit/verify` for a sample of tenants, and run one analysis per critical tenant.
6. Restore workspace snapshots if uploads are used; other repositories are re-fetched at the next scan.
7. Re-point DNS / the GitHub App webhook URL; webhooks missed during the outage are recovered by re-syncing pull requests (`POST /api/v1/graph/rescan`
   and re-running analyses).

RTO target 4 hours: steps 2–5 are dominated by the database restore time, which the operator should measure with a quarterly restore drill and
record here.

## 5. Routine operations

| Task | Frequency | Command |
|---|---|---|
| Retention | daily | `python -m tia.retention enforce` |
| Restore drill (tenant export into a scratch database, `verify`) | quarterly | `python -m tia.backup export / restore / verify` |
| Audit chain check | daily, alert on failure | `GET /api/v1/audit/verify` per tenant |
| Usage export for billing | monthly | `python -m tia.tenants meter <tenant> --format csv` |
| Key rotation | yearly or on suspicion | `TIA_MASTER_KEY_PREVIOUS` + `python -m tia.secretstore`; customer keys: `python -m tia.tenants key-set` |
| Upgrade | per release | stop workers → `python -m tia.migrate` → start API and workers |

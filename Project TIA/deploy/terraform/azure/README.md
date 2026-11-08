# TIA SaaS region on Azure (Terraform)

Written for the platform team standing up the first production region.

**Status: not validated.** These files were written and reviewed by hand. They have not been run through `terraform validate` or applied:
no Terraform binary or Azure subscription was available when they were written. Expect to fix provider-version details on first `plan`.

## What it creates

| Need (PRD) | Resource |
|---|---|
| Gateway, WAF, rate limits (PL-009, SECURITY T17) | Front Door Premium + WAF (default rule set, bot rules, per-IP rate limit); TIA also limits per principal and tenant |
| Horizontal scale, no single point of failure (PL-004) | AKS across 3 zones, autoscaling node pools; the chart runs 2+ API and 4+ worker replicas with a PodDisruptionBudget |
| Managed PostgreSQL with HA and PITR (PL-004, PL-005) | Flexible Server 16, zone-redundant HA, private access only, TLS 1.2 required, 35-day PITR, optional geo-redundant backup |
| Secrets and keys outside the database (PL-011, PL-016) | Key Vault Premium, HSM-backed key-encryption key with rotation, workload identity for the pods |
| Shared workspace (clones, uploads) | Azure Files premium NFS share, ZRS, private endpoint |
| Monitoring (PL-007) | Log Analytics + Container Insights; PostgreSQL logs and metrics |
| Data residency (PL-015) | One state file and one region per `region_code`; `geo_redundant_backup = false` where backups must stay in-region |

## Steps

1. `terraform init` (with a remote state backend), then `terraform apply -var region_code=eu -var location=westeurope`.
2. Finish the edge: create the Front Door origin group and origin pointing (via Private Link) at the AKS internal ingress load balancer, a route
   for the custom domain, and a security policy binding the WAF policy to the endpoint. These depend on the ingress controller you choose.
3. Bootstrap the database once (from a pod or jump host in the VNet): `python -m tia.migrate` with the admin DSN from Key Vault (`admin-dsn`).
   It creates the `tia_app` and `tia_worker` roles. Store their DSNs, the audit key, the metrics token and `TIA_BACKUP_KEY` as Key Vault
   secrets, and mount them with the CSI driver into the `tia-secrets` Kubernetes secret.
4. Install the chart: `helm install tia deploy/helm/tia -n tia -f deploy/helm/tia/values-region-eu.yaml`, with the service account annotated
   with `workload_identity_client_id` and the workspace share mounted as the workspace volume.
5. Set `TIA_TRUST_PROXY=true` (the API is only reachable through Front Door) and `TIA_RATE_LIMIT_REPLICAS` to the API replica count.
6. Run the checks in [OPERATIONS.md](../../../docs/OPERATIONS.md): `/readyz`, an audit verification, one analysis, and a restore drill
   before taking customers.

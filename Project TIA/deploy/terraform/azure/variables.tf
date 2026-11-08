variable "region_code" {
  description = "Short data-residency region code; must match the deployment's TIA_REGION (eu, uk, us, ...)."
  type        = string
  validation {
    condition     = contains(["eu", "us", "apac", "uk", "ca", "au", "in", "jp", "ch", "sa"], var.region_code)
    error_message = "region_code must be one of TIA's regions (tia/saas.py REGIONS)."
  }
}

variable "location" {
  description = "Azure region with three availability zones, e.g. westeurope, uksouth, eastus2."
  type        = string
}

variable "environment" {
  description = "prod, staging, ..."
  type        = string
  default     = "prod"
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "vnet_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "namespace" {
  description = "Kubernetes namespace the chart is installed in (its service account 'tia' gets the workload identity)."
  type        = string
  default     = "tia"
}

variable "kubernetes_version" {
  type    = string
  default = "1.30"
}

variable "system_vm_size" {
  type    = string
  default = "Standard_D4ds_v5"
}

variable "work_vm_size" {
  description = "Nodes for the API and workers (extraction is CPU-bound)."
  type        = string
  default     = "Standard_D8ds_v5"
}

variable "work_min_nodes" {
  type    = number
  default = 3
}

variable "work_max_nodes" {
  type    = number
  default = 12
}

variable "postgres_sku" {
  type    = string
  default = "GP_Standard_D8ds_v5"
}

variable "postgres_storage_mb" {
  type    = number
  default = 524288
}

variable "postgres_storage_tier" {
  type    = string
  default = "P30"
}

variable "geo_redundant_backup" {
  description = "Backups replicated to the paired region. Set false where residency forbids leaving the region (then rely on zone redundancy)."
  type        = bool
  default     = true
}

variable "workspace_quota_gb" {
  type    = number
  default = 1024
}

variable "log_retention_days" {
  type    = number
  default = 90
}

variable "edge_rate_limit_per_minute" {
  description = "Per client IP at Front Door; TIA additionally limits per principal and per tenant (tia/ratelimit.py)."
  type        = number
  default     = 1200
}

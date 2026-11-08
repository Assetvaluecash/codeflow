# TIA SaaS - one region on Azure (PRD PL-009, PL-004, PL-005, PL-007, PL-011, PL-015; docs/OPERATIONS.md).
#
#   Edge        Azure Front Door Premium + WAF policy: managed rule set, per-client rate limit, TLS 1.2+, only path to the API
#   Compute     AKS (system + workload node pools across 3 zones, autoscaling, Azure CNI overlay, workload identity, Key Vault CSI driver)
#   Data        PostgreSQL Flexible Server 16, zone-redundant HA, private VNet access only, geo-redundant backups (35 days PITR)
#   Workspace   Azure Files premium share (ReadWriteMany) for clones and uploads, private endpoint, snapshots by the backup vault
#   Keys        Key Vault Premium (HSM-backed key-encryption key for tenant data keys, purge protection) + secrets for the chart
#   Telemetry   Log Analytics + Container Insights; TIA exports OTLP traces to the collector of your choice
# Then the Helm chart (deploy/helm/tia) is installed with values-region-<x>.yaml, reading its secrets from Key Vault.
#
# Status: written and reviewed by hand; NOT validated with `terraform validate` or applied (no Terraform or Azure account was available).

terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.8" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
  }
  # backend "azurerm" { resource_group_name = "tia-tfstate" storage_account_name = "<name>" container_name = "tfstate" key = "tia-<region>.tfstate" }
}

provider "azurerm" {
  features {
    key_vault { purge_soft_delete_on_destroy = false }
  }
}

locals {
  name = "tia-${var.region_code}-${var.environment}"
  tags = merge({ product = "tia", region = var.region_code, environment = var.environment, data_residency = var.region_code }, var.tags)
}

data "azurerm_client_config" "current" {}

resource "azurerm_resource_group" "rg" {
  name     = "rg-${local.name}"
  location = var.location
  tags     = local.tags
}

# ---- network --------------------------------------------------------------------------------------------------------------------------
resource "azurerm_virtual_network" "vnet" {
  name                = "vnet-${local.name}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  address_space       = [var.vnet_cidr]
  tags                = local.tags
}

resource "azurerm_subnet" "aks" {
  name                 = "snet-aks"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = [cidrsubnet(var.vnet_cidr, 4, 0)]
}

resource "azurerm_subnet" "postgres" {
  name                 = "snet-postgres"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = [cidrsubnet(var.vnet_cidr, 8, 32)]
  service_endpoints    = ["Microsoft.Storage"]
  delegation {
    name = "pg"
    service_delegation {
      name    = "Microsoft.DBforPostgreSQL/flexibleServers"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_subnet" "endpoints" {
  name                 = "snet-endpoints"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = [cidrsubnet(var.vnet_cidr, 8, 33)]
}

resource "azurerm_private_dns_zone" "pg" {
  name                = "${local.name}.private.postgres.database.azure.com"
  resource_group_name = azurerm_resource_group.rg.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "pg" {
  name                  = "pg-link"
  resource_group_name   = azurerm_resource_group.rg.name
  private_dns_zone_name = azurerm_private_dns_zone.pg.name
  virtual_network_id    = azurerm_virtual_network.vnet.id
}

# ---- telemetry --------------------------------------------------------------------------------------------------------------------------
resource "azurerm_log_analytics_workspace" "logs" {
  name                = "log-${local.name}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  tags                = local.tags
}

# ---- keys and secrets ---------------------------------------------------------------------------------------------------------------------
resource "azurerm_key_vault" "kv" {
  name                          = substr(replace("kv-${local.name}", "-", ""), 0, 24)
  location                      = azurerm_resource_group.rg.location
  resource_group_name           = azurerm_resource_group.rg.name
  tenant_id                     = data.azurerm_client_config.current.tenant_id
  sku_name                      = "premium"
  purge_protection_enabled      = true
  soft_delete_retention_days    = 90
  enable_rbac_authorization     = true
  public_network_access_enabled = false
  network_acls {
    default_action = "Deny"
    bypass         = "AzureServices"
  }
  tags = local.tags
}

resource "azurerm_key_vault_key" "kek" {
  name         = "tia-kek"
  key_vault_id = azurerm_key_vault.kv.id
  key_type     = "RSA-HSM"
  key_size     = 3072
  key_opts     = ["wrapKey", "unwrapKey"]
  rotation_policy {
    automatic {
      time_before_expiry = "P30D"
    }
    expire_after         = "P1Y"
    notify_before_expiry = "P45D"
  }
}

resource "azurerm_private_endpoint" "kv" {
  name                = "pe-kv-${local.name}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  subnet_id           = azurerm_subnet.endpoints.id
  private_service_connection {
    name                           = "kv"
    private_connection_resource_id = azurerm_key_vault.kv.id
    subresource_names              = ["vault"]
    is_manual_connection           = false
  }
  tags = local.tags
}

# ---- PostgreSQL ---------------------------------------------------------------------------------------------------------------------------
resource "random_password" "pg_admin" {
  length  = 32
  special = false
}

resource "azurerm_postgresql_flexible_server" "pg" {
  name                          = "psql-${local.name}"
  location                      = azurerm_resource_group.rg.location
  resource_group_name           = azurerm_resource_group.rg.name
  version                       = "16"
  sku_name                      = var.postgres_sku
  storage_mb                    = var.postgres_storage_mb
  storage_tier                  = var.postgres_storage_tier
  auto_grow_enabled             = true
  delegated_subnet_id           = azurerm_subnet.postgres.id
  private_dns_zone_id           = azurerm_private_dns_zone.pg.id
  public_network_access_enabled = false
  administrator_login           = "tia_owner"
  administrator_password        = random_password.pg_admin.result
  backup_retention_days         = 35
  geo_redundant_backup_enabled  = var.geo_redundant_backup
  zone                          = "1"
  high_availability {
    mode                      = "ZoneRedundant"
    standby_availability_zone = "2"
  }
  maintenance_window {
    day_of_week  = 0
    start_hour   = 2
    start_minute = 0
  }
  tags       = local.tags
  depends_on = [azurerm_private_dns_zone_virtual_network_link.pg]
}

resource "azurerm_postgresql_flexible_server_configuration" "tls" {
  name      = "require_secure_transport"
  server_id = azurerm_postgresql_flexible_server.pg.id
  value     = "on"
}

resource "azurerm_postgresql_flexible_server_configuration" "tls_min" {
  name      = "ssl_min_protocol_version"
  server_id = azurerm_postgresql_flexible_server.pg.id
  value     = "TLSv1.2"
}

resource "azurerm_postgresql_flexible_server_database" "tia" {
  name      = "tia"
  server_id = azurerm_postgresql_flexible_server.pg.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

resource "azurerm_monitor_diagnostic_setting" "pg" {
  name                       = "pg-logs"
  target_resource_id         = azurerm_postgresql_flexible_server.pg.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.logs.id
  enabled_log { category = "PostgreSQLLogs" }
  metric { category = "AllMetrics" }
}

# ---- workspace volume (clones and uploads) ------------------------------------------------------------------------------------------------
resource "azurerm_storage_account" "ws" {
  name                            = substr(replace("st${local.name}", "-", ""), 0, 24)
  location                        = azurerm_resource_group.rg.location
  resource_group_name             = azurerm_resource_group.rg.name
  account_tier                    = "Premium"
  account_kind                    = "FileStorage"
  account_replication_type        = "ZRS"
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = false # NFS shares require it off; access is private-endpoint only
  public_network_access_enabled   = false
  allow_nested_items_to_be_public = false
  tags                            = local.tags
}

resource "azurerm_storage_share" "ws" {
  name               = "tia-workspace"
  storage_account_id = azurerm_storage_account.ws.id
  quota              = var.workspace_quota_gb
  enabled_protocol   = "NFS"
}

resource "azurerm_private_endpoint" "ws" {
  name                = "pe-st-${local.name}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  subnet_id           = azurerm_subnet.endpoints.id
  private_service_connection {
    name                           = "files"
    private_connection_resource_id = azurerm_storage_account.ws.id
    subresource_names              = ["file"]
    is_manual_connection           = false
  }
  tags = local.tags
}

# ---- AKS --------------------------------------------------------------------------------------------------------------------------------
resource "azurerm_kubernetes_cluster" "aks" {
  name                              = "aks-${local.name}"
  location                          = azurerm_resource_group.rg.location
  resource_group_name               = azurerm_resource_group.rg.name
  dns_prefix                        = local.name
  kubernetes_version                = var.kubernetes_version
  sku_tier                          = "Standard"
  oidc_issuer_enabled               = true
  workload_identity_enabled         = true
  role_based_access_control_enabled = true
  local_account_disabled            = true
  azure_active_directory_role_based_access_control {
    azure_rbac_enabled = true
    tenant_id          = data.azurerm_client_config.current.tenant_id
  }
  default_node_pool {
    name                 = "system"
    vm_size              = var.system_vm_size
    vnet_subnet_id       = azurerm_subnet.aks.id
    zones                = ["1", "2", "3"]
    auto_scaling_enabled = true
    min_count            = 3
    max_count            = 5
    only_critical_addons_enabled = true
    upgrade_settings { max_surge = "33%" }
  }
  identity { type = "SystemAssigned" }
  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    network_policy      = "cilium"
    network_data_plane  = "cilium"
    outbound_type       = "loadBalancer"
  }
  key_vault_secrets_provider { secret_rotation_enabled = true }
  oms_agent { log_analytics_workspace_id = azurerm_log_analytics_workspace.logs.id }
  automatic_upgrade_channel = "patch"
  tags                      = local.tags
}

resource "azurerm_kubernetes_cluster_node_pool" "work" {
  name                  = "work"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.aks.id
  vm_size               = var.work_vm_size
  vnet_subnet_id        = azurerm_subnet.aks.id
  zones                 = ["1", "2", "3"]
  auto_scaling_enabled  = true
  min_count             = var.work_min_nodes
  max_count             = var.work_max_nodes
  tags                  = local.tags
}

resource "azurerm_user_assigned_identity" "tia" {
  name                = "id-${local.name}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  tags                = local.tags
}

resource "azurerm_federated_identity_credential" "tia" {
  name                = "tia-workload"
  resource_group_name = azurerm_resource_group.rg.name
  parent_id           = azurerm_user_assigned_identity.tia.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = azurerm_kubernetes_cluster.aks.oidc_issuer_url
  subject             = "system:serviceaccount:${var.namespace}:tia"
}

resource "azurerm_role_assignment" "tia_secrets" {
  scope                = azurerm_key_vault.kv.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.tia.principal_id
}

resource "azurerm_role_assignment" "tia_crypto" {
  scope                = azurerm_key_vault_key.kek.resource_versionless_id
  role_definition_name = "Key Vault Crypto User"
  principal_id         = azurerm_user_assigned_identity.tia.principal_id
}

# ---- edge: Front Door Premium + WAF (rate limits in addition to TIA's own limiter) ----------------------------------------------------------
resource "azurerm_cdn_frontdoor_profile" "fd" {
  name                = "afd-${local.name}"
  resource_group_name = azurerm_resource_group.rg.name
  sku_name            = "Premium_AzureFrontDoor"
  tags                = local.tags
}

resource "azurerm_cdn_frontdoor_firewall_policy" "waf" {
  name                = replace("waf${local.name}", "-", "")
  resource_group_name = azurerm_resource_group.rg.name
  sku_name            = azurerm_cdn_frontdoor_profile.fd.sku_name
  mode                = "Prevention"
  managed_rule {
    type    = "Microsoft_DefaultRuleSet"
    version = "2.1"
    action  = "Block"
  }
  managed_rule {
    type    = "Microsoft_BotManagerRuleSet"
    version = "1.1"
    action  = "Block"
  }
  custom_rule {
    name                           = "PerClientRate"
    priority                       = 10
    type                           = "RateLimitRule"
    action                         = "Block"
    rate_limit_duration_in_minutes = 1
    rate_limit_threshold           = var.edge_rate_limit_per_minute
    match_condition {
      match_variable = "RequestUri"
      operator       = "RegEx"
      match_values   = ["^/(api|auth|analyses|mcp)/?"]
    }
  }
  tags = local.tags
}

resource "azurerm_cdn_frontdoor_endpoint" "api" {
  name                     = "fde-${local.name}"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.fd.id
  tags                     = local.tags
}
# The origin group, origin (the AKS ingress, via Private Link to its internal load balancer), route, custom domain and the security policy
# binding the WAF to the endpoint depend on the ingress controller chosen; see README.md in this folder.

# ---- secrets the chart needs (values are set once by the operator; Terraform never stores TIA's own keys) -----------------------------------
resource "azurerm_key_vault_secret" "admin_dsn" {
  name         = "admin-dsn"
  key_vault_id = azurerm_key_vault.kv.id
  value        = "postgresql://tia_owner:${random_password.pg_admin.result}@${azurerm_postgresql_flexible_server.pg.fqdn}:5432/tia?sslmode=verify-full"
  content_type = "postgresql dsn (owner; used only by tia.migrate, tia.tenants, tia.retention, tia.backup)"
}

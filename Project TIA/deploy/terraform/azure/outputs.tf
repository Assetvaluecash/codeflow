output "resource_group" {
  value = azurerm_resource_group.rg.name
}

output "aks_name" {
  value = azurerm_kubernetes_cluster.aks.name
}

output "postgres_fqdn" {
  value = azurerm_postgresql_flexible_server.pg.fqdn
}

output "key_vault_uri" {
  value = azurerm_key_vault.kv.vault_uri
}

output "kek_id" {
  description = "Key-encryption key for tenant data keys; give TIA azure-kv:<this id> as the default key (TIA_AZURE_* or workload identity)."
  value       = azurerm_key_vault_key.kek.versionless_id
}

output "workload_identity_client_id" {
  description = "Annotate the chart's service account with azure.workload.identity/client-id = this value."
  value       = azurerm_user_assigned_identity.tia.client_id
}

output "workspace_share" {
  value = "${azurerm_storage_account.ws.name}/${azurerm_storage_share.ws.name}"
}

output "frontdoor_endpoint" {
  value = azurerm_cdn_frontdoor_endpoint.api.host_name
}

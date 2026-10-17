output "api_url" {
  value = "https://${azurerm_container_app.api.ingress[0].fqdn}"
}

output "dashboard_url" {
  value = "https://${azurerm_static_web_app.dashboard.default_host_name}"
}

output "dashboard_deployment_token" {
  value     = azurerm_static_web_app.dashboard.api_key
  sensitive = true
}

output "storage_account" {
  value = azurerm_storage_account.lake.name
}

output "lakehouse_root" {
  value = "abfs://lakehouse@${azurerm_storage_account.lake.name}.dfs.core.windows.net/lakehouse"
}

output "postgres_fqdn" {
  value = azurerm_postgresql_flexible_server.db.fqdn
}

output "resource_group" {
  value = azurerm_resource_group.rg.name
}

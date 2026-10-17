# Azure deployment: a storage account with hierarchical namespace for the lakehouse (Delta tables read by
# delta-rs over abfs://), a PostgreSQL flexible server for the audit log and summaries, the API as a
# Container App, the Angular dashboard as a Static Web App, and Log Analytics for the container logs.
# Everything here is in the free tier or the cheapest SKU; the Postgres server is the only line item that
# always costs money (about $13/month on B1ms) and is destroyed by `terraform destroy`.

terraform {
  required_version = ">= 1.9"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.20" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "azurerm" {
  features {
    resource_group { prevent_deletion_if_contains_resources = false }
  }
  subscription_id = var.subscription_id
}

locals {
  name = "${var.project}-${var.environment}"
  tags = { project = var.project, environment = var.environment, managed_by = "terraform" }
}

resource "random_string" "suffix" {
  length  = 6
  upper   = false
  special = false
}

resource "random_password" "db" {
  length  = 24
  special = false
}

resource "azurerm_resource_group" "rg" {
  name     = "rg-${local.name}"
  location = var.location
  tags     = local.tags
}

resource "azurerm_log_analytics_workspace" "logs" {
  name                = "log-${local.name}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.tags
}

# ---- lakehouse storage -------------------------------------------------------------------------------------
resource "azurerm_storage_account" "lake" {
  name                     = replace("st${var.project}${random_string.suffix.result}", "-", "")
  resource_group_name      = azurerm_resource_group.rg.name
  location                 = azurerm_resource_group.rg.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  is_hns_enabled           = true
  min_tls_version          = "TLS1_2"
  tags                     = local.tags
}

resource "azurerm_storage_container" "lakehouse" {
  name                  = "lakehouse"
  storage_account_id    = azurerm_storage_account.lake.id
  container_access_type = "private"
}

# ---- audit log store ---------------------------------------------------------------------------------------
resource "azurerm_postgresql_flexible_server" "db" {
  name                          = "pg-${local.name}-${random_string.suffix.result}"
  resource_group_name           = azurerm_resource_group.rg.name
  location                      = azurerm_resource_group.rg.location
  version                       = "16"
  administrator_login           = "cfp"
  administrator_password        = random_password.db.result
  sku_name                      = var.postgres_sku
  storage_mb                    = 32768
  backup_retention_days         = 7
  zone                          = "1"
  public_network_access_enabled = true
  tags                          = local.tags
}

resource "azurerm_postgresql_flexible_server_database" "cfp" {
  name      = "cfp"
  server_id = azurerm_postgresql_flexible_server.db.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

# Container Apps have no fixed egress IPs on the consumption plan; this rule admits Azure services.
resource "azurerm_postgresql_flexible_server_firewall_rule" "azure" {
  name             = "allow-azure-services"
  server_id        = azurerm_postgresql_flexible_server.db.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

# ---- API ---------------------------------------------------------------------------------------------------
resource "azurerm_container_app_environment" "env" {
  name                       = "cae-${local.name}"
  location                   = azurerm_resource_group.rg.location
  resource_group_name        = azurerm_resource_group.rg.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.logs.id
  tags                       = local.tags
}

resource "azurerm_container_app" "api" {
  name                         = "ca-${local.name}-api"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Single"
  tags                         = local.tags

  dynamic "registry" {
    for_each = toset(var.registry_server == "" ? [] : ["registry"])
    content {
      server               = var.registry_server
      username             = var.registry_username
      password_secret_name = "registry-password"
    }
  }

  secret {
    name  = "database-url"
    value = "postgresql+psycopg://cfp:${random_password.db.result}@${azurerm_postgresql_flexible_server.db.fqdn}:5432/cfp?sslmode=require"
  }
  secret {
    name  = "api-keys"
    value = var.api_keys
  }
  secret {
    name  = "storage-key"
    value = azurerm_storage_account.lake.primary_access_key
  }
  secret {
    name  = "deid-salt"
    value = var.deid_salt
  }
  dynamic "secret" {
    for_each = toset(var.registry_server == "" ? [] : ["registry"])
    content {
      name  = "registry-password"
      value = var.registry_password
    }
  }
  dynamic "secret" {
    for_each = toset(var.llm_api_key == "" ? [] : ["llm"])
    content {
      name  = "llm-api-key"
      value = var.llm_api_key
    }
  }

  ingress {
    external_enabled = true
    target_port      = 8000
    transport        = "http"
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = 0
    max_replicas = 2
    container {
      name   = "api"
      image  = var.api_image
      cpu    = 0.5
      memory = "1Gi"

      env {
        name        = "CFP_DATABASE_URL"
        secret_name = "database-url"
      }
      env {
        name        = "CFP_API_KEYS"
        secret_name = "api-keys"
      }
      env {
        name        = "AZURE_STORAGE_ACCOUNT_KEY"
        secret_name = "storage-key"
      }
      env {
        name        = "CFP_DEID_SALT"
        secret_name = "deid-salt"
      }
      env {
        name  = "AZURE_STORAGE_ACCOUNT_NAME"
        value = azurerm_storage_account.lake.name
      }
      env {
        name  = "CFP_LAKEHOUSE_ROOT"
        value = "abfs://lakehouse@${azurerm_storage_account.lake.name}.dfs.core.windows.net/lakehouse"
      }
      env {
        name  = "CFP_CORS_ORIGINS"
        value = "https://${azurerm_static_web_app.dashboard.default_host_name}"
      }
      env {
        name  = "CFP_LLM_BASE_URL"
        value = var.llm_base_url
      }
      env {
        name  = "CFP_LLM_MODEL"
        value = var.llm_model
      }
      dynamic "env" {
        for_each = toset(var.llm_api_key == "" ? [] : ["llm"])
        content {
          name        = "CFP_LLM_API_KEY"
          secret_name = "llm-api-key"
        }
      }
      env {
        name  = "CFP_RATE_LIMIT"
        value = var.rate_limit
      }

      liveness_probe {
        transport = "HTTP"
        path      = "/health"
        port      = 8000
      }
      readiness_probe {
        transport = "HTTP"
        path      = "/health"
        port      = 8000
      }
    }
  }
}

# ---- dashboard ---------------------------------------------------------------------------------------------
resource "azurerm_static_web_app" "dashboard" {
  name                = "swa-${local.name}"
  resource_group_name = azurerm_resource_group.rg.name
  location            = var.swa_location
  sku_tier            = "Free"
  sku_size            = "Free"
  tags                = local.tags
}

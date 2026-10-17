variable "subscription_id" {
  description = "Azure subscription to deploy into"
  type        = string
}

variable "project" {
  type    = string
  default = "cfp"
}

variable "environment" {
  type    = string
  default = "demo"
}

variable "location" {
  description = "Region for the resource group, storage, Postgres and Container Apps"
  type        = string
  default     = "eastus2"
}

variable "swa_location" {
  description = "Static Web Apps are offered in a few regions only"
  type        = string
  default     = "eastus2"
}

variable "postgres_sku" {
  type    = string
  default = "B_Standard_B1ms"
}

variable "api_image" {
  description = "API image, e.g. ghcr.io/<owner>/clinical-fhir-pipeline/api:<tag>"
  type        = string
}

variable "registry_server" {
  description = "Registry host when the image is private (ghcr.io); empty for a public image"
  type        = string
  default     = ""
}

variable "registry_username" {
  type    = string
  default = ""
}

variable "registry_password" {
  type      = string
  default   = ""
  sensitive = true
}

variable "api_keys" {
  description = "key:role pairs, comma separated (roles analyst and admin)"
  type        = string
  sensitive   = true
}

variable "deid_salt" {
  description = "Secret salt used by the pipeline that wrote the lakehouse (kept with the API so a rerun matches)"
  type        = string
  sensitive   = true
}

variable "llm_base_url" {
  description = "OpenAI-compatible endpoint for summaries; empty = deterministic fake model"
  type        = string
  default     = ""
}

variable "llm_model" {
  type    = string
  default = "qwen2.5:3b"
}

variable "llm_api_key" {
  type      = string
  default   = ""
  sensitive = true
}

variable "rate_limit" {
  type    = string
  default = "60/minute"
}

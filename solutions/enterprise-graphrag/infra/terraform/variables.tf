variable "project_id" {
  type        = string
  description = "Target Google Cloud project ID."
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid 6-30 character GCP project ID."
  }
}

variable "region" {
  type        = string
  default     = "us-central1"
  description = "Primary Google Cloud region for Cloud Run, Artifact Registry, GCS, and Model Armor."
}

variable "bq_location" {
  type        = string
  default     = "US"
  description = "BigQuery multi-region or region for the acc05_enterprise_graphrag dataset."
}

variable "app_env" {
  type        = string
  default     = "prod"
  description = "Deployment environment label (dev, demo, staging, prod)."
  validation {
    condition     = can(regex("^[a-z][a-z0-9_-]{1,31}$", var.app_env))
    error_message = "app_env must be lowercase alphanumeric/underscore/hyphen (2-32 chars)."
  }
}

variable "container_image" {
  type        = string
  description = "Full Artifact Registry container image URI built from Dockerfile."
}

variable "bq_dataset" {
  type        = string
  default     = "acc05_enterprise_graphrag"
  description = "BigQuery dataset ID following the acc05_ naming prefix standard."
  validation {
    condition     = can(regex("^[a-z0-9_]{2,40}$", var.bq_dataset))
    error_message = "bq_dataset must match ^[a-z0-9_]{2,40}$."
  }
}

variable "model_armor_template_id" {
  type        = string
  default     = "acc05-enterprise-graphrag-guardrail"
  description = "Dedicated Google Cloud Model Armor template ID for prompt screening."
}

variable "access_mode" {
  type        = string
  default     = "iap"
  description = "Access mode for live Cloud Run service: 'iap' (recommended) or 'public'."
  validation {
    condition     = contains(["iap", "public"], var.access_mode)
    error_message = "access_mode must be 'iap' or 'public'."
  }
}

variable "iap_members" {
  type        = list(string)
  default     = []
  description = "IAM members granted roles/iap.httpsResourceAccessor when access_mode = 'iap' (e.g., user:you@company.com)."
}

variable "deploy_replay_service" {
  type        = bool
  default     = true
  description = "Deploy a separate public zero-credential Replay Cloud Run service (acc05-graphrag-replay)."
}

variable "enable_cloud_dlp" {
  type        = bool
  default     = false
  description = "Enable Cloud DLP PII inspection on customer document uploads."
}

variable "enable_enterprise_scale" {
  type        = bool
  default     = false
  description = "Tier-2 Enterprise toggle: provision Spanner Graph + warm Cloud Run min_instances=1."
}

variable "spanner_processing_units" {
  type        = number
  default     = 100
  description = "Tier-2 Spanner Graph compute capacity (100-10000 PU; ignored when enable_enterprise_scale = false)."
  validation {
    condition     = var.spanner_processing_units >= 100 && var.spanner_processing_units <= 10000
    error_message = "spanner_processing_units must be between 100 and 10000."
  }
}

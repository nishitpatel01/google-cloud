# Dedicated least-privilege runtime service account (zero JSON key files)
resource "google_service_account" "runtime" {
  account_id   = local.runtime_sa_id
  display_name = "ACC-05 Enterprise GraphRAG Live Runtime"
  description  = "Least-privilege Cloud Run runtime SA for ACC-05 (portfolio=gcp_ai_accelerators)."
}

# Role-less service account for the zero-credential Replay service
resource "google_service_account" "replay" {
  count        = var.deploy_replay_service ? 1 : 0
  account_id   = local.replay_sa_id
  display_name = "ACC-05 Enterprise GraphRAG Replay (Role-less)"
  description  = "Zero-privilege Cloud Run SA for offline Replay mode (portfolio=gcp_ai_accelerators)."
}

locals {
  runtime_project_roles = toset([
    "roles/aiplatform.user",
    "roles/bigquery.jobUser",
    "roles/discoveryengine.viewer",
    "roles/modelarmor.user",
    "roles/dlp.user",
    "roles/logging.logWriter",
    "roles/cloudtrace.agent",
  ])
}

resource "google_project_iam_member" "runtime_roles" {
  for_each = local.runtime_project_roles
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.runtime.email}"
}

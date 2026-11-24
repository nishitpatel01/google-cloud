# Artifact Registry repository for ACC-05 container builds
resource "google_artifact_registry_repository" "acc05_repo" {
  repository_id = local.artifact_repo_id
  location      = var.region
  format        = "DOCKER"
  description   = "Container images for ACC-05 Risk & Contagion Intelligence (Enterprise GraphRAG)."
  labels        = local.common_labels

  depends_on = [google_project_service.tier1]
}

# Private customer document upload bucket (UBLA + PAP enforced + 90-day lifecycle)
resource "google_storage_bucket" "uploads" {
  name                        = local.upload_bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = local.common_labels

  lifecycle_rule {
    condition {
      age = 90
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.tier1]
}

resource "google_storage_bucket_iam_member" "runtime_uploads_admin" {
  bucket = google_storage_bucket.uploads.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

# Tier-1 Serverless BigQuery Property Graph dataset (6-key labeled, dataset-scoped SA access)
resource "google_bigquery_dataset" "acc05_graph" {
  dataset_id    = var.bq_dataset
  friendly_name = "ACC-05 Enterprise GraphRAG Property Graph"
  description   = "Homogeneous Entity/Rel Property Graph and source chunks for ACC-05."
  location      = var.bq_location
  labels        = local.common_labels

  depends_on = [google_project_service.tier1]
}

resource "google_bigquery_dataset_iam_member" "runtime_dataset_editor" {
  dataset_id = google_bigquery_dataset.acc05_graph.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.runtime.email}"
}

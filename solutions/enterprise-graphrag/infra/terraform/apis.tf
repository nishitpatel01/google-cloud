locals {
  tier1_apis = toset([
    "run.googleapis.com",
    "aiplatform.googleapis.com",
    "bigquery.googleapis.com",
    "bigqueryreservation.googleapis.com",
    "discoveryengine.googleapis.com",
    "storage.googleapis.com",
    "modelarmor.googleapis.com",
    "dlp.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudasset.googleapis.com",
    "iap.googleapis.com",
  ])
}

resource "google_project_service" "tier1" {
  for_each           = local.tier1_apis
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_project_service" "tier2_spanner" {
  count              = var.enable_enterprise_scale ? 1 : 0
  project            = var.project_id
  service            = "spanner.googleapis.com"
  disable_on_destroy = false
}

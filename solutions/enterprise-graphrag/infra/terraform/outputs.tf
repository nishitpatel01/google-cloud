output "live_service_url" {
  description = "Live Cloud Run service URL (IAP-protected when access_mode = 'iap')."
  value       = google_cloud_run_v2_service.live.uri
}

output "replay_service_url" {
  description = "Public zero-credential Replay Cloud Run service URL (when deploy_replay_service = true)."
  value       = var.deploy_replay_service ? google_cloud_run_v2_service.replay[0].uri : null
}

output "runtime_service_account" {
  description = "Least-privilege runtime service account email."
  value       = google_service_account.runtime.email
}

output "upload_bucket" {
  description = "Private GCS upload bucket name (UBLA + PAP enforced)."
  value       = google_storage_bucket.uploads.name
}

output "bigquery_dataset" {
  description = "Labeled BigQuery Property Graph dataset ID."
  value       = google_bigquery_dataset.acc05_graph.dataset_id
}

output "applied_labels" {
  description = "Mandatory 6-key GCP resource labels applied to all resources."
  value       = local.common_labels
}

data "google_project" "current" {
  project_id = var.project_id
}

# Live Cloud Run v2 Service (Tier-1 scale-to-zero default; warm min_instances=1 in Tier-2)
resource "google_cloud_run_v2_service" "live" {
  provider     = google-beta
  name         = local.live_service_name
  location     = var.region
  ingress      = "INGRESS_TRAFFIC_ALL"
  iap_enabled  = var.access_mode == "iap"
  labels       = local.common_labels

  template {
    service_account = google_service_account.runtime.email
    timeout         = "600s"
    labels          = local.common_labels

    scaling {
      min_instance_count = var.enable_enterprise_scale ? 1 : 0
      max_instance_count = var.enable_enterprise_scale ? 5 : 2
    }

    containers {
      image = var.container_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.enable_enterprise_scale ? "2" : "1"
          memory = "2Gi"
        }
        cpu_idle = !var.enable_enterprise_scale
      }

      startup_probe {
        http_get {
          path = "/health"
        }
        initial_delay_seconds = 2
        period_seconds        = 5
        failure_threshold     = 10
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "GCP_REGION"
        value = var.region
      }
      env {
        name  = "APP_ENV"
        value = var.app_env
      }
      env {
        name  = "BQ_DATASET"
        value = var.bq_dataset
      }
      env {
        name  = "BQ_LOCATION"
        value = var.bq_location
      }
      env {
        name  = "GCS_BUCKET"
        value = google_storage_bucket.uploads.name
      }
      env {
        name  = "DEMO_MODE"
        value = "live"
      }
      env {
        name  = "GRAPH_BACKEND"
        value = var.enable_enterprise_scale ? "spanner" : "bigquery"
      }
      env {
        name  = "SPANNER_INSTANCE_ID"
        value = var.enable_enterprise_scale ? local.spanner_instance_id : ""
      }
      env {
        name  = "SPANNER_DATABASE_ID"
        value = local.spanner_database_id
      }
      env {
        name  = "MODEL_ARMOR_LOCATION"
        value = var.region
      }
      env {
        name  = "MODEL_ARMOR_TEMPLATE_ID"
        value = var.model_armor_template_id
      }
      env {
        name  = "ENABLE_CLOUD_DLP"
        value = tostring(var.enable_cloud_dlp)
      }
      env {
        name  = "REQUIRE_IAP_JWT"
        value = tostring(var.access_mode == "iap")
      }
      env {
        name  = "IAP_JWT_AUDIENCE"
        value = var.access_mode == "iap" ? "/projects/${data.google_project.current.number}/locations/${var.region}/services/${local.live_service_name}" : ""
      }
    }
  }

  depends_on = [
    google_project_service.tier1,
    google_project_iam_member.runtime_roles,
    google_bigquery_dataset_iam_member.runtime_dataset_editor,
    google_storage_bucket_iam_member.runtime_uploads_admin,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "live_public_invoker" {
  count    = var.access_mode == "public" ? 1 : 0
  name     = google_cloud_run_v2_service.live.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "live_iap_service_agent" {
  count    = var.access_mode == "iap" ? 1 : 0
  name     = google_cloud_run_v2_service.live.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-iap.iam.gserviceaccount.com"
}

resource "google_iap_web_cloud_run_service_iam_member" "live_iap_access" {
  provider               = google-beta
  for_each               = var.access_mode == "iap" ? toset(var.iap_members) : toset([])
  project                = var.project_id
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.live.name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.value
}

# Optional Zero-Credential Public Replay Service (role-less SA, 33 live-recorded fixtures)
resource "google_cloud_run_v2_service" "replay" {
  count    = var.deploy_replay_service ? 1 : 0
  name     = local.replay_service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"
  labels   = local.common_labels

  template {
    service_account = google_service_account.replay[0].email
    timeout         = "180s"
    labels          = local.common_labels

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    containers {
      image = var.container_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
        cpu_idle = true
      }

      startup_probe {
        http_get {
          path = "/health"
        }
        initial_delay_seconds = 2
        period_seconds        = 5
        failure_threshold     = 10
      }

      env {
        name  = "DEMO_MODE"
        value = "replay"
      }
      env {
        name  = "APP_ENV"
        value = var.app_env
      }
      env {
        name  = "GRAPH_BACKEND"
        value = "bigquery"
      }
    }
  }

  depends_on = [google_project_service.tier1]
}

resource "google_cloud_run_v2_service_iam_member" "replay_public_invoker" {
  count    = var.deploy_replay_service ? 1 : 0
  name     = google_cloud_run_v2_service.replay[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

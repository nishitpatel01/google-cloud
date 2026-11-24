# Tier-2 Enterprise Toggle (Spanner Graph): strictly parked at $0/mo when enable_enterprise_scale = false

resource "google_spanner_instance" "acc05_graph" {
  count            = var.enable_enterprise_scale ? 1 : 0
  name             = local.spanner_instance_id
  config           = "regional-${var.region}"
  display_name     = "ACC-05 Enterprise GraphRAG (Tier-2 Spanner Graph)"
  processing_units = var.spanner_processing_units
  labels           = local.common_labels

  depends_on = [google_project_service.tier2_spanner]
}

resource "google_spanner_database" "acc05_graph_db" {
  count               = var.enable_enterprise_scale ? 1 : 0
  instance            = google_spanner_instance.acc05_graph[0].name
  name                = local.spanner_database_id
  deletion_protection = false

  ddl = [
    for stmt in split(";", file("${path.module}/../sql/spanner_schema.sql")) :
    trimspace(stmt)
    if length(trimspace(stmt)) > 0 && !startswith(trimspace(stmt), "--")
  ]
}

resource "google_spanner_database_iam_member" "runtime_spanner_user" {
  count    = var.enable_enterprise_scale ? 1 : 0
  instance = google_spanner_instance.acc05_graph[0].name
  database = google_spanner_database.acc05_graph_db[0].name
  role     = "roles/spanner.databaseUser"
  member   = "serviceAccount:${google_service_account.runtime.email}"
}

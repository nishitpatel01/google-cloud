locals {
  solution_id   = "acc05"
  solution_slug = "enterprise_graphrag"
  portfolio_id  = "gcp_ai_accelerators"
  tier_label    = var.enable_enterprise_scale ? "tier2_enterprise" : "tier1_serverless"

  # Mandatory 6-key GCP FinOps & Discovery Label Standard applied across every resource
  common_labels = {
    portfolio   = local.portfolio_id
    solution_id = local.solution_id
    solution    = local.solution_slug
    tier        = local.tier_label
    managed_by  = "terraform"
    env         = var.app_env
  }

  upload_bucket_name  = "${var.project_id}-acc05-graphrag-uploads"
  artifact_repo_id    = "acc05-graphrag"
  live_service_name   = "acc05-graphrag-ui"
  replay_service_name = "acc05-graphrag-replay"
  runtime_sa_id       = "acc05-graphrag-run"
  replay_sa_id        = "acc05-graphrag-replay"
  spanner_instance_id = "acc05-graphrag-spanner"
  spanner_database_id = "acc05-graphrag-db"
}

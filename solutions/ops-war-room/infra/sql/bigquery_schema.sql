-- Operations & Incident War Room (ACC-04) — BigQuery Operational Telemetry Schema
-- Placeholders ${PROJECT_ID} and ${DATASET_ID} (default: acc04_ops_war_room) are substituted by scripts/seed_data.py.

CREATE SCHEMA IF NOT EXISTS `${PROJECT_ID}.${DATASET_ID}`
OPTIONS (
  location = "US",
  description = "ACC-04 Operations & Incident War Room telemetry, public intel, runbooks, and remediation audit logs",
  labels = [
    ("portfolio", "gcp_ai_accelerators"),
    ("solution_id", "acc04"),
    ("solution", "ops_war_room"),
    ("tier", "tier1_serverless"),
    ("managed_by", "seed_script"),
    ("env", "dev")
  ]
);

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${DATASET_ID}.incidents` (
  incident_id STRING NOT NULL,
  pack_id STRING NOT NULL,
  title STRING NOT NULL,
  summary STRING NOT NULL,
  severity STRING NOT NULL,
  sla_minutes_remaining INT64 NOT NULL,
  expected_verdict STRING NOT NULL,
  expected_root_cause STRING NOT NULL,
  expected_event_ids ARRAY<STRING>,
  expected_intel_ids ARRAY<STRING>,
  expected_action_type STRING NOT NULL,
  has_prompt_injection BOOL NOT NULL,
  financial_exposure_usd FLOAT64 NOT NULL,
  remediation_options_json STRING NOT NULL,
  data_origin STRING NOT NULL
)
CLUSTER BY pack_id, severity, incident_id
OPTIONS (
  description = "Curated multi-domain operational incidents and golden evaluation ground truth",
  labels = [
    ("portfolio", "gcp_ai_accelerators"),
    ("solution_id", "acc04"),
    ("solution", "ops_war_room"),
    ("tier", "tier1_serverless"),
    ("managed_by", "seed_script"),
    ("env", "dev")
  ]
);

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${DATASET_ID}.telemetry_events` (
  event_id STRING NOT NULL,
  pack_id STRING NOT NULL,
  incident_id STRING NOT NULL,
  event_ts TIMESTAMP NOT NULL,
  source_table STRING NOT NULL,
  actor STRING NOT NULL,
  resource STRING NOT NULL,
  action STRING NOT NULL,
  status STRING NOT NULL,
  severity STRING NOT NULL,
  raw_message STRING NOT NULL,
  structured_fields_json STRING NOT NULL,
  contains_prompt_injection BOOL NOT NULL,
  data_origin STRING NOT NULL
)
PARTITION BY DATE(event_ts)
CLUSTER BY pack_id, incident_id, source_table
OPTIONS (
  description = "Read-only operational logs (Cloud Audit, VPC Flow, ERP/WMS/EDI, Cloud Logging/Metrics)",
  labels = [
    ("portfolio", "gcp_ai_accelerators"),
    ("solution_id", "acc04"),
    ("solution", "ops_war_room"),
    ("tier", "tier1_serverless"),
    ("managed_by", "seed_script"),
    ("env", "dev")
  ]
);

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${DATASET_ID}.intel_catalog` (
  intel_id STRING NOT NULL,
  pack_id STRING NOT NULL,
  intel_type STRING NOT NULL,
  title STRING NOT NULL,
  summary STRING NOT NULL,
  source_name STRING NOT NULL,
  source_url STRING NOT NULL,
  license_name STRING NOT NULL,
  metadata_json STRING NOT NULL,
  data_origin STRING NOT NULL
)
CLUSTER BY pack_id, intel_type, intel_id
OPTIONS (
  description = "Real public intelligence catalog (MITRE ATT&CK STIX, CISA KEV, NWS Alerts, UN/LOCODE, GCP Status, OTel)",
  labels = [
    ("portfolio", "gcp_ai_accelerators"),
    ("solution_id", "acc04"),
    ("solution", "ops_war_room"),
    ("tier", "tier1_serverless"),
    ("managed_by", "seed_script"),
    ("env", "dev")
  ]
);

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${DATASET_ID}.runbooks` (
  runbook_id STRING NOT NULL,
  pack_id STRING NOT NULL,
  title STRING NOT NULL,
  category STRING NOT NULL,
  content STRING NOT NULL,
  safe_commands ARRAY<STRING>,
  data_origin STRING NOT NULL
)
CLUSTER BY pack_id, category
OPTIONS (
  description = "Grounded SOPs and response playbooks (NIST SP 800-61r3, O2C fulfillment policy, Google SRE burn-rate runbooks)",
  labels = [
    ("portfolio", "gcp_ai_accelerators"),
    ("solution_id", "acc04"),
    ("solution", "ops_war_room"),
    ("tier", "tier1_serverless"),
    ("managed_by", "seed_script"),
    ("env", "dev")
  ]
);

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${DATASET_ID}.remediation_audit` (
  execution_id STRING NOT NULL,
  incident_id STRING NOT NULL,
  pack_id STRING NOT NULL,
  option_id STRING NOT NULL,
  plan_sha256 STRING NOT NULL,
  idempotency_key STRING NOT NULL,
  approver_identity STRING NOT NULL,
  workflow_name STRING NOT NULL,
  final_state STRING NOT NULL,
  compensation_triggered BOOL NOT NULL,
  steps_json STRING NOT NULL,
  executed_at TIMESTAMP NOT NULL
)
PARTITION BY DATE(executed_at)
CLUSTER BY pack_id, incident_id
OPTIONS (
  description = "Immutable append-only audit trail of two-click approved remediations and Cloud Workflows sagas",
  labels = [
    ("portfolio", "gcp_ai_accelerators"),
    ("solution_id", "acc04"),
    ("solution", "ops_war_room"),
    ("tier", "tier1_serverless"),
    ("managed_by", "seed_script"),
    ("env", "dev")
  ]
);

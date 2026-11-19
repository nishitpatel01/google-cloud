-- Tier-1 Serverless BigQuery & BigQuery Property Graph (ISO/IEC 39075 GQL) Schema
-- Substitutions performed safely via validated identifiers during scripts/seed_data.py:
--   ${PROJECT_ID}, ${BQ_DATASET}, ${BQ_LOCATION}, ${BQ_RESERVATION_ID}
--
-- Note: BigQuery Property Graph requires an ENTERPRISE (or ENTERPRISE_PLUS) reservation.
-- Provisioned with slot_capacity = 0 and autoscale_max_slots = 50 -> $0/mo while idle.

-- 0. Idempotent 0-baseline Enterprise reservation (executed conditionally if no assignment exists)
-- CREATE RESERVATION `${PROJECT_ID}.region-${BQ_LOCATION_LOWER}.${BQ_RESERVATION_ID}`
--   OPTIONS (edition = 'ENTERPRISE', slot_capacity = 0, autoscale_max_slots = 50);
-- CREATE ASSIGNMENT `${PROJECT_ID}.region-${BQ_LOCATION_LOWER}.${BQ_RESERVATION_ID}.${BQ_RESERVATION_ID}-query`
--   OPTIONS (assignee = 'projects/${PROJECT_ID}', job_type = 'QUERY');

CREATE SCHEMA IF NOT EXISTS `${PROJECT_ID}.${BQ_DATASET}`
OPTIONS (location = '${BQ_LOCATION}');

-- 1. Homogeneous Entity Vertices (scoped by pack_id for zero-DDL multi-pack & BYO-ontology support)
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${BQ_DATASET}.entities` (
  pack_id STRING NOT NULL,
  entity_id STRING NOT NULL,
  canonical_name STRING NOT NULL,
  entity_type STRING NOT NULL,
  jurisdiction STRING,
  risk_tier STRING NOT NULL,
  exposure_usd FLOAT64 DEFAULT 0.0,
  single_source BOOL DEFAULT FALSE,
  data_origin STRING NOT NULL,           -- 'REAL_PUBLIC' | 'MODELED_BRIDGE'
  source_license STRING NOT NULL,        -- 'US_GOV_PUBLIC_DOMAIN' | 'CC0_1_0' | 'CC_BY_4_0' | 'MODELED'
  source_url STRING,
  attributes_json JSON,
  source_chunk_ids ARRAY<STRING>,
  embedding ARRAY<FLOAT64>,              -- gemini-embedding-2 (excluded from Property Graph DDL)
  ingest_run_id STRING NOT NULL,
  updated_at TIMESTAMP NOT NULL
)
CLUSTER BY pack_id, entity_type;

-- 2. Directed Relationships carrying explicit chunk provenance
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${BQ_DATASET}.edges` (
  pack_id STRING NOT NULL,
  edge_id STRING NOT NULL,
  source_entity_id STRING NOT NULL,
  target_entity_id STRING NOT NULL,
  relation_type STRING NOT NULL,
  weight FLOAT64 DEFAULT 1.0,
  exposure_usd FLOAT64 DEFAULT 0.0,
  confidence FLOAT64 DEFAULT 1.0,
  evidence_excerpt STRING NOT NULL,
  source_chunk_id STRING NOT NULL,
  source_doc_name STRING NOT NULL,
  data_origin STRING NOT NULL,
  source_license STRING NOT NULL,
  source_url STRING,
  ingest_run_id STRING NOT NULL
)
CLUSTER BY pack_id, relation_type;

-- 3. Source Document Chunks shared by Flat Vector RAG baseline and GraphRAG evidence inspection
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${BQ_DATASET}.source_chunks` (
  pack_id STRING NOT NULL,
  chunk_id STRING NOT NULL,
  doc_name STRING NOT NULL,
  doc_type STRING NOT NULL,
  page_number INT64 NOT NULL,
  content STRING NOT NULL,
  data_origin STRING NOT NULL,
  source_license STRING NOT NULL,
  source_url STRING,
  embedding ARRAY<FLOAT64>,
  ingest_run_id STRING NOT NULL
)
CLUSTER BY pack_id, doc_type;

-- 4. Ingestion & Query Provenance Tables
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${BQ_DATASET}.ingest_runs` (
  run_id STRING NOT NULL,
  pack_id STRING NOT NULL,
  started_at TIMESTAMP NOT NULL,
  finished_at TIMESTAMP,
  status STRING NOT NULL,
  docs_count INT64,
  entities_count INT64,
  edges_count INT64,
  chunks_count INT64,
  embedding_model STRING,
  stage_log JSON
);

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${BQ_DATASET}.query_runs` (
  run_id STRING NOT NULL,
  pack_id STRING NOT NULL,
  engine_id STRING NOT NULL,             -- 'flat_vector_rag' | 'bq_graphrag' | 'spanner_graphrag'
  question_id STRING,
  query STRING NOT NULL,
  started_at TIMESTAMP NOT NULL,
  latency_ms INT64,
  engine_cost_usd FLOAT64,
  judge_cost_usd FLOAT64,
  groundedness FLOAT64,
  path_recall FLOAT64,
  qa_relevance FLOAT64,
  provenance_verified BOOL,
  demo_mode STRING NOT NULL,
  stage_log JSON
);

-- 5. BigQuery Property Graph (ISO/IEC 39075 GQL)
-- Dataset-qualified unbackticked references + alias-only REFERENCES + scalar-only PROPERTIES.
CREATE OR REPLACE PROPERTY GRAPH ${BQ_DATASET}.EnterpriseGraph
  NODE TABLES (
    ${BQ_DATASET}.entities AS Entity
      KEY (pack_id, entity_id)
      LABEL Entity
      PROPERTIES (
        pack_id,
        entity_id,
        canonical_name,
        entity_type,
        jurisdiction,
        risk_tier,
        exposure_usd,
        single_source,
        data_origin,
        source_license
      )
  )
  EDGE TABLES (
    ${BQ_DATASET}.edges AS Rel
      KEY (pack_id, edge_id)
      SOURCE KEY (pack_id, source_entity_id) REFERENCES Entity (pack_id, entity_id)
      DESTINATION KEY (pack_id, target_entity_id) REFERENCES Entity (pack_id, entity_id)
      LABEL Rel
      PROPERTIES (
        pack_id,
        edge_id,
        source_entity_id,
        target_entity_id,
        relation_type,
        weight,
        exposure_usd,
        confidence,
        evidence_excerpt,
        source_chunk_id,
        source_doc_name,
        data_origin,
        source_license
      )
  );

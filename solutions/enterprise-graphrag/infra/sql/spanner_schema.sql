-- Tier-2 Enterprise Spanner Graph (ISO/IEC 39075 GQL) Schema
-- Provisioned ONLY when Terraform variable enable_enterprise_scale = true.
-- Mirrors the exact homogeneous Entity/Rel contract of BigQuery Property Graph.

CREATE TABLE entities (
  pack_id STRING(64) NOT NULL,
  entity_id STRING(128) NOT NULL,
  canonical_name STRING(256) NOT NULL,
  entity_type STRING(64) NOT NULL,
  jurisdiction STRING(64),
  risk_tier STRING(32) NOT NULL,
  exposure_usd FLOAT64 NOT NULL DEFAULT (0.0),
  single_source BOOL NOT NULL DEFAULT (FALSE),
  data_origin STRING(32) NOT NULL,
  source_license STRING(32) NOT NULL,
  source_url STRING(512),
  attributes_json JSON,
  embedding ARRAY<FLOAT32>(vector_length=>3072),
  ingest_run_id STRING(64) NOT NULL,
  updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)
) PRIMARY KEY (pack_id, entity_id);

CREATE TABLE edges (
  pack_id STRING(64) NOT NULL,
  edge_id STRING(128) NOT NULL,
  source_entity_id STRING(128) NOT NULL,
  target_entity_id STRING(128) NOT NULL,
  relation_type STRING(64) NOT NULL,
  weight FLOAT64 NOT NULL DEFAULT (1.0),
  exposure_usd FLOAT64 NOT NULL DEFAULT (0.0),
  confidence FLOAT64 NOT NULL DEFAULT (1.0),
  evidence_excerpt STRING(2048) NOT NULL,
  source_chunk_id STRING(128) NOT NULL,
  source_doc_name STRING(256) NOT NULL,
  data_origin STRING(32) NOT NULL,
  source_license STRING(32) NOT NULL,
  source_url STRING(512),
  ingest_run_id STRING(64) NOT NULL,
  CONSTRAINT fk_edge_source FOREIGN KEY (pack_id, source_entity_id)
    REFERENCES entities (pack_id, entity_id),
  CONSTRAINT fk_edge_target FOREIGN KEY (pack_id, target_entity_id)
    REFERENCES entities (pack_id, entity_id)
) PRIMARY KEY (pack_id, edge_id);

CREATE INDEX idx_edges_source ON edges (pack_id, source_entity_id, relation_type);
CREATE INDEX idx_edges_target ON edges (pack_id, target_entity_id, relation_type);

CREATE TABLE source_chunks (
  pack_id STRING(64) NOT NULL,
  chunk_id STRING(128) NOT NULL,
  doc_name STRING(256) NOT NULL,
  doc_type STRING(64) NOT NULL,
  page_number INT64 NOT NULL,
  content STRING(MAX) NOT NULL,
  data_origin STRING(32) NOT NULL,
  source_license STRING(32) NOT NULL,
  source_url STRING(512),
  embedding ARRAY<FLOAT32>(vector_length=>3072),
  ingest_run_id STRING(64) NOT NULL
) PRIMARY KEY (pack_id, chunk_id);

CREATE OR REPLACE PROPERTY GRAPH EnterpriseGraph
  NODE TABLES (
    entities AS Entity
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
    edges AS Rel
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

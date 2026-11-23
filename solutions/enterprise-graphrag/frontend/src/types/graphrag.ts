export type DataOrigin = "REAL_PUBLIC" | "MODELED_BRIDGE" | "CUSTOMER_UPLOAD";
export type RiskTier = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
export type TraversalDirection = "outgoing" | "incoming" | "both";
export type EngineId =
  | "bq_graphrag"
  | "adk_graph_coordinator"
  | "flat_vector_rag"
  | "spanner_graphrag";

export interface ModelSpec {
  model_id: string;
  display_name: string;
  family: "gemini" | "claude";
  tier: "ga_default" | "frontier_opt_in";
  input_per_1m_usd: number;
  output_per_1m_usd: number;
  supports_adk: boolean;
}

export interface MetaResponse {
  app_version: string;
  demo_mode: "replay" | "record" | "live";
  graph_backend: "bigquery" | "spanner";
  default_synthesis_model: string;
  default_judge_model: string;
  embedding_model: string;
  model_armor_configured: boolean;
  dlp_enabled: boolean;
  iap_required: boolean;
  spanner_configured: boolean;
  live_capable: boolean;
  models: ModelSpec[];
}

export interface OntologyNodeType {
  type: string;
  label: string;
  color: string;
  shape: "circle" | "hexagon" | "square" | "diamond";
  is_revenue_bearing: boolean;
}

export interface OntologyEdgeType {
  type: string;
  label: string;
  directed: boolean;
  propagates_risk: boolean;
}

export interface OntologySpec {
  node_types: OntologyNodeType[];
  edge_types: OntologyEdgeType[];
  dlp_info_types: string[];
}

export interface GoldenQuestion {
  question_id: string;
  question: string;
  hop_depth: number;
  direction: TraversalDirection;
  seed_entity_ids: string[];
  expected_entity_ids: string[];
  expected_edge_ids: string[];
  expected_answer_points: string[];
  why_flat_vector_misses: string;
}

export interface PackSummary {
  pack_id: string;
  title: string;
  subtitle: string;
  industry: string;
  persona: string;
  manual_baseline_cost_usd: number;
  manual_baseline_minutes: number;
  default_max_hops: number;
  primary_sources: string[];
  real_public_overall_ratio: number;
  counts: {
    entities: number;
    edges: number;
    chunks: number;
    questions: number;
  };
  ontology: OntologySpec;
  questions: GoldenQuestion[];
}

export interface EntityNode {
  pack_id: string;
  entity_id: string;
  canonical_name: string;
  entity_type: string;
  jurisdiction: string;
  risk_tier: RiskTier;
  exposure_usd: number;
  single_source: boolean;
  data_origin: DataOrigin;
  source_license: string;
  source_url: string;
  source_chunk_ids: string[];
  attributes: Record<string, unknown>;
}

export interface GraphEdge {
  pack_id: string;
  edge_id: string;
  source_entity_id: string;
  target_entity_id: string;
  relation_type: string;
  weight: number;
  exposure_usd: number;
  confidence: number;
  evidence_excerpt: string;
  source_chunk_id: string;
  source_doc_name: string;
  data_origin: DataOrigin;
  source_license: string;
  source_url: string;
}

export interface SourceChunk {
  pack_id: string;
  chunk_id: string;
  doc_name: string;
  doc_type: string;
  page_number: number;
  content: string;
  data_origin: DataOrigin;
  source_license: string;
  source_url: string;
}

export interface PackGraphResponse {
  pack_id: string;
  ontology: OntologySpec;
  nodes: EntityNode[];
  edges: GraphEdge[];
  chunks: SourceChunk[];
  truncated: boolean;
}

export interface GraphPath {
  path_id: string;
  hop_count: number;
  node_ids: string[];
  edge_ids: string[];
  path_exposure_usd: number;
  min_confidence: number;
}

export interface ProvenanceReport {
  cited_entity_ids: string[];
  verified_entity_ids: string[];
  unverified_entity_ids: string[];
  cited_edge_ids: string[];
  verified_edge_ids: string[];
  unverified_edge_ids: string[];
  cited_chunk_ids: string[];
  verified_chunk_ids: string[];
  unverified_chunk_ids: string[];
}

export interface StageEvent {
  event_type: "stage_started" | "stage_completed" | "stage_failed" | "stage_skipped";
  stage: string;
  label?: string;
  service?: string;
  service_used?: string;
  duration_ms?: number;
  request?: Record<string, unknown>;
  payload?: Record<string, unknown>;
  error_type?: string;
  error_message?: string;
  reason?: string;
  arm?: string;
  engine_id?: string;
}

export interface RecallMetrics {
  entity_recall: number;
  edge_recall: number;
  path_recall: number;
}

export interface EngineResultPayload {
  engine_id: string;
  engine_label: string;
  pack_id: string;
  query: string;
  synthesis_model: string;
  answer: string;
  paths: GraphPath[];
  retrieved_entity_ids: string[];
  retrieved_edge_ids: string[];
  retrieved_chunk_ids: string[];
  retrieved_nodes: Record<string, unknown>[];
  retrieved_edges: Record<string, unknown>[];
  retrieved_chunks: Record<string, unknown>[];
  provenance: ProvenanceReport;
  usage: {
    input_tokens: number;
    output_tokens: number;
  };
  engine_cost_usd: number;
  total_duration_ms: number;
  flags: {
    graph_backend_used: string;
    reranker_used: boolean;
    model_armor_checked: boolean;
    bq_cache_hit: boolean;
    bytes_processed: number;
  };
  recall: RecallMetrics | null;
}

export interface ComparisonPayload {
  pack_id: string;
  question_id: string | null;
  flat_vector_rag: EngineResultPayload;
  graphrag: EngineResultPayload;
  path_recall_delta: number | null;
}

export interface IngestUploadResponse {
  status: string;
  extraction_mode: string;
  message?: string;
  upload: {
    pack_id: string;
    display_name: string;
    canonical_mime: string;
    gcs_object_key: string;
    size_bytes: number;
    sha256: string;
    is_image: boolean;
  };
  dlp: {
    enabled: boolean;
    inspected_chars: number;
    findings_count: number;
    info_types_found: string[];
    counts_by_type: Record<string, number>;
    reason: string;
  };
  model_armor_checked?: boolean;
  chunks_count: number;
  merged_entities_count?: number;
  dropped_invalid_count?: number;
  extraction_cost_usd?: number;
  entities: EntityNode[];
  edges: GraphEdge[];
  persisted: Record<string, unknown> | null;
}

export interface TcoEstimateResponse {
  tier: "tier1_bigquery_serverless" | "tier2_spanner_enterprise";
  monthly_queries: number;
  synthesis_model: string;
  llm_monthly_usd: number;
  graph_compute_monthly_usd: number;
  cloud_run_and_ops_monthly_usd: number;
  total_gcp_monthly_usd: number;
  unit_cost_per_query_usd: number;
  manual_baseline_monthly_usd: number;
  net_monthly_savings_usd: number;
  roi_multiple: number;
}

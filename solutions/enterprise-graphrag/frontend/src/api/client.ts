import type {
  ComparisonPayload,
  EngineId,
  EngineResultPayload,
  IngestUploadResponse,
  MetaResponse,
  PackGraphResponse,
  PackSummary,
  ReleasesResponse,
  StageEvent,
  TcoEstimateResponse,
  TraversalDirection,
} from "../types/graphrag";
import { parseSseBuffer } from "../utils/format";

async function jsonRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, init);
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const body = (await resp.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // Keep status fallback
    }
    throw new Error(detail);
  }
  return (await resp.json()) as T;
}

export function fetchMeta(): Promise<MetaResponse> {
  return jsonRequest<MetaResponse>("/api/v1/meta");
}

export function fetchReleases(): Promise<ReleasesResponse> {
  return jsonRequest<ReleasesResponse>("/api/v1/releases");
}

export async function fetchPacks(): Promise<PackSummary[]> {
  const res = await jsonRequest<{ packs: PackSummary[] }>("/api/v1/packs");
  return res.packs;
}

export function fetchPackGraph(packId: string): Promise<PackGraphResponse> {
  return jsonRequest<PackGraphResponse>(`/api/v1/packs/${encodeURIComponent(packId)}/graph`);
}

export function estimateTco(body: {
  pack_id?: string;
  monthly_queries: number;
  synthesis_model: string;
  tier: "tier1_bigquery_serverless" | "tier2_spanner_enterprise";
  spanner_processing_units?: number;
}): Promise<TcoEstimateResponse> {
  return jsonRequest<TcoEstimateResponse>("/api/v1/tco/estimate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function uploadDocument(params: {
  packId: string;
  file: File;
  customOntologyYaml?: string;
  persistToBigquery?: boolean;
  extractionModel?: string;
}): Promise<IngestUploadResponse> {
  const form = new FormData();
  form.append("pack_id", params.packId);
  form.append("file", params.file);
  if (params.customOntologyYaml?.trim()) {
    form.append("custom_ontology_yaml", params.customOntologyYaml);
  }
  form.append("persist_to_bigquery", String(Boolean(params.persistToBigquery)));
  if (params.extractionModel) {
    form.append("extraction_model", params.extractionModel);
  }
  return jsonRequest<IngestUploadResponse>("/api/v1/ingest/upload", {
    method: "POST",
    body: form,
  });
}

async function consumeSse(
  path: string,
  payload: Record<string, unknown>,
  onEvent: (eventName: string, data: unknown) => void,
  signal?: AbortSignal
): Promise<void> {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(payload),
    signal,
  });
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const body = (await resp.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // Keep status fallback
    }
    throw new Error(detail);
  }
  if (!resp.body) {
    throw new Error("Streaming response body is unavailable");
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parsed = parseSseBuffer(buffer);
    buffer = parsed.remainder;
    for (const ev of parsed.events) {
      onEvent(ev.event, ev.data);
    }
  }
  if (buffer.trim()) {
    const tail = parseSseBuffer(buffer + "\n\n");
    for (const ev of tail.events) {
      onEvent(ev.event, ev.data);
    }
  }
}

export async function streamQuery(
  params: {
    pack_id: string;
    query: string;
    engine_id: EngineId;
    synthesis_model: string;
    question_id?: string | null;
    max_hops: number;
    direction: TraversalDirection;
    enable_rerank: boolean;
    mode: "replay" | "live";
  },
  callbacks: {
    onStage: (ev: StageEvent) => void;
    onResult: (res: EngineResultPayload) => void;
    onError: (msg: string) => void;
  },
  signal?: AbortSignal
): Promise<void> {
  await consumeSse(
    "/api/v1/query/stream",
    params,
    (eventName, data) => {
      if (eventName === "stage") {
        callbacks.onStage(data as StageEvent);
      } else if (eventName === "result") {
        callbacks.onResult(data as EngineResultPayload);
      } else if (eventName === "error") {
        const err = data as { message?: string };
        callbacks.onError(err.message || "Query stream error");
      }
    },
    signal
  );
}

export async function streamCompare(
  params: {
    pack_id: string;
    query: string;
    graph_engine_id: "bq_graphrag" | "adk_graph_coordinator" | "spanner_graphrag";
    synthesis_model: string;
    question_id?: string | null;
    max_hops: number;
    direction: TraversalDirection;
    enable_rerank: boolean;
    mode: "replay" | "live";
  },
  callbacks: {
    onStage: (ev: StageEvent) => void;
    onComparison: (res: ComparisonPayload) => void;
    onError: (msg: string) => void;
  },
  signal?: AbortSignal
): Promise<void> {
  await consumeSse(
    "/api/v1/compare/stream",
    params,
    (eventName, data) => {
      if (eventName === "stage") {
        callbacks.onStage(data as StageEvent);
      } else if (eventName === "comparison") {
        callbacks.onComparison(data as ComparisonPayload);
      } else if (eventName === "error") {
        const err = data as { message?: string };
        callbacks.onError(err.message || "Comparison stream error");
      }
    },
    signal
  );
}

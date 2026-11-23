"""Multimodal ontology-guided graph extractor (PDF/Text/CSV/Image -> canonical entities & edges)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Sequence

import yaml
from pydantic import ValidationError

from app.core.attribution import gcp_job_labels
from app.core.config import MODEL_CATALOG, Settings
from app.core.gcp_clients import get_genai_client, retry_with_backoff
from app.core.models import (
    DataOrigin,
    EntityNode,
    GraphEdge,
    OntologySpec,
    RiskTier,
    SourceChunk,
    SourceLicense,
)
from app.core.pricing import TokenUsage, compute_llm_cost_usd
from app.core.security import screen_with_model_armor
from app.ingestion.upload_validator import ValidatedUpload
from app.packs.entity_resolver import remap_edges, resolve_entities

MAX_PDF_PAGES = 40
CHUNK_SIZE_CHARS = 2400
CHUNK_OVERLAP_CHARS = 200
MAX_EXTRACTION_CHARS = 24_000

_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def _safe_slug(val: str, max_len: int = 28) -> str:
    cleaned = _SLUG_RE.sub("-", (val or "").upper()).strip("-")
    return (cleaned or "ITEM")[:max_len]


def _make_id(prefix: str, seed: str, label: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8].upper()
    return f"{prefix}-{_safe_slug(label)}-{digest}"


def parse_custom_ontology(raw_yaml_or_json: str | None, fallback: OntologySpec) -> OntologySpec:
    """Safely parse optional BYO ontology YAML/JSON (yaml.safe_load only) into OntologySpec."""
    if not raw_yaml_or_json or not raw_yaml_or_json.strip():
        return fallback
    parsed = yaml.safe_load(raw_yaml_or_json)
    if not isinstance(parsed, dict):
        raise ValueError("Custom ontology must be a YAML/JSON object with node_types and edge_types")
    # Accept either top-level {node_types, edge_types} or nested {ontology: {...}}
    candidate = parsed.get("ontology") if isinstance(parsed.get("ontology"), dict) else parsed
    return OntologySpec.model_validate(candidate)


def _slice_text(text: str, *, chunk_size: int = CHUNK_SIZE_CHARS, overlap: int = CHUNK_OVERLAP_CHARS) -> list[str]:
    clean = text.strip()
    if not clean:
        return []
    if len(clean) <= chunk_size:
        return [clean]
    out: list[str] = []
    start = 0
    step = max(200, chunk_size - overlap)
    while start < len(clean):
        part = clean[start : start + chunk_size].strip()
        if len(part) >= 20:
            out.append(part)
        if start + chunk_size >= len(clean):
            break
        start += step
    return out


def extract_document_chunks(upload: ValidatedUpload) -> tuple[tuple[SourceChunk, ...], bool]:
    """Extract bounded text chunks from PDF/text/CSV/YAML or create image metadata chunk."""
    short_hash = upload.sha256[:8].upper()
    chunks: list[SourceChunk] = []

    if upload.extension in (".png", ".jpg", ".jpeg"):
        chunks.append(
            SourceChunk(
                pack_id=upload.pack_id,
                chunk_id=f"UPL-IMG-{short_hash}-001",
                doc_name=upload.display_name,
                doc_type="uploaded_image",
                page_number=1,
                content=(
                    f"Uploaded image document '{upload.display_name}' ({upload.canonical_mime}, "
                    f"{upload.size_bytes} bytes, sha256={upload.sha256[:16]}). "
                    "Visual entities and relationships extracted via multimodal Gemini vision."
                ),
                data_origin=DataOrigin.CUSTOMER_UPLOAD,
                source_license=SourceLicense.CUSTOMER_PRIVATE,
            )
        )
        return tuple(chunks), True

    if upload.extension == ".pdf":
        import pymupdf

        doc = pymupdf.open(stream=upload.content, filetype="pdf")
        try:
            idx = 0
            for page_idx in range(min(len(doc), MAX_PDF_PAGES)):
                page_text = (doc[page_idx].get_text("text") or "").strip()
                for snippet in _slice_text(page_text):
                    idx += 1
                    chunks.append(
                        SourceChunk(
                            pack_id=upload.pack_id,
                            chunk_id=f"UPL-CHK-{short_hash}-{idx:03d}",
                            doc_name=upload.display_name,
                            doc_type="uploaded_pdf",
                            page_number=page_idx + 1,
                            content=snippet,
                            data_origin=DataOrigin.CUSTOMER_UPLOAD,
                            source_license=SourceLicense.CUSTOMER_PRIVATE,
                        )
                    )
        finally:
            doc.close()
        if not chunks:
            raise ValueError("Uploaded PDF contains no extractable text (consider uploading a PNG/JPEG page image)")
        return tuple(chunks), False

    # UTF-8 text / markdown / csv / yaml
    decoded = upload.content.decode("utf-8")
    for idx, snippet in enumerate(_slice_text(decoded), start=1):
        chunks.append(
            SourceChunk(
                pack_id=upload.pack_id,
                chunk_id=f"UPL-CHK-{short_hash}-{idx:03d}",
                doc_name=upload.display_name,
                doc_type=f"uploaded_{upload.extension.lstrip('.')}",
                page_number=1,
                content=snippet,
                data_origin=DataOrigin.CUSTOMER_UPLOAD,
                source_license=SourceLicense.CUSTOMER_PRIVATE,
            )
        )
    if not chunks:
        raise ValueError("Uploaded text document is too short (< 20 characters)")
    return tuple(chunks), False


@dataclass(frozen=True)
class ExtractionOutcome:
    """Validated, deduplicated graph extraction output from an uploaded customer document."""

    chunks: tuple[SourceChunk, ...]
    entities: tuple[EntityNode, ...]
    edges: tuple[GraphEdge, ...]
    merged_entities_count: int
    dropped_invalid_count: int
    model_armor_checked: bool
    usage: TokenUsage
    extraction_cost_usd: float


def _invoke_gemini_json_extractor(
    *,
    settings: Settings,
    model_id: str,
    prompt_text: str,
    image_bytes: bytes | None = None,
    image_mime: str | None = None,
) -> tuple[dict[str, Any], TokenUsage]:
    """Call Vertex AI Gemini with strict JSON response mode for ontology-guided extraction."""
    spec = MODEL_CATALOG.get(model_id)
    if spec is None or spec.family != "gemini":
        raise ValueError(f"Extraction requires a Vertex AI Gemini model, got {model_id!r}")

    from google.genai import types

    client = get_genai_client(settings.gcp_project_id, location=settings.vertex_location)
    parts: list[Any] = [types.Part.from_text(text=prompt_text)]
    if image_bytes is not None and image_mime:
        parts.append(types.Part.from_bytes(data=image_bytes, mime_type=image_mime))

    def _call() -> tuple[dict[str, Any], TokenUsage]:
        resp = client.models.generate_content(
            model=model_id,
            contents=types.Content(role="user", parts=parts),
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
                max_output_tokens=4096,
                labels=gcp_job_labels(settings, component="multimodal_extractor"),
            ),
        )
        raw_text = (resp.text or "").strip()
        if not raw_text:
            raise RuntimeError(f"Empty JSON extraction response from {model_id}")
        parsed = json.loads(raw_text)
        if not isinstance(parsed, dict):
            raise RuntimeError("Gemini extraction did not return a JSON object")
        um = getattr(resp, "usage_metadata", None)
        usage = TokenUsage(
            input_tokens=int(getattr(um, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(um, "candidates_token_count", 0) or 0),
        )
        return parsed, usage

    return retry_with_backoff(_call)


def extract_graph_from_upload(
    *,
    settings: Settings,
    upload: ValidatedUpload,
    ontology: OntologySpec,
    existing_entities: Sequence[EntityNode] = (),
    extraction_model: str | None = None,
    extractor_fn: Any | None = None,
) -> ExtractionOutcome:
    """Extract ontology-valid entities & edges from an upload and resolve against existing graph."""
    chunks, is_image = extract_document_chunks(upload)
    combined_text = "\n\n".join(f"[{c.chunk_id} p.{c.page_number}]\n{c.content}" for c in chunks)

    # Screen untrusted uploaded text with Model Armor when configured
    ma_verdict = screen_with_model_armor(
        settings=settings,
        text=combined_text[:16_000],
        mode="user_prompt",
    )

    model_id = extraction_model or settings.vertex_model_synthesis
    allowed_nodes = sorted(ontology.allowed_node_types)
    allowed_edges = sorted(ontology.allowed_edge_types)
    chunk_ids_valid = {c.chunk_id for c in chunks}
    default_chunk_id = chunks[0].chunk_id

    prompt = (
        "Extract a knowledge graph strictly matching the ontology below from the uploaded document.\n"
        f"Allowed entity_type values: {json.dumps(allowed_nodes)}\n"
        f"Allowed relation_type values: {json.dumps(allowed_edges)}\n"
        "Allowed risk_tier values: [\"LOW\", \"MEDIUM\", \"HIGH\", \"CRITICAL\"]\n"
        "Return ONLY JSON matching this exact schema:\n"
        "{\n"
        '  "entities": [{"canonical_name": "...", "entity_type": "...", "jurisdiction": "UNKNOWN", '
        '"risk_tier": "MEDIUM", "exposure_usd": 0.0, "single_source": false, "source_chunk_id": "..."}],\n'
        '  "edges": [{"source_name": "...", "target_name": "...", "relation_type": "...", '
        '"exposure_usd": 0.0, "confidence": 0.85, "evidence_excerpt": "...", "source_chunk_id": "..."}]\n'
        "}\n"
        "Rules: use ONLY allowed entity_type and relation_type values; every edge must cite an exact "
        "verbatim evidence_excerpt (>= 15 chars) from the document and a valid source_chunk_id; "
        "do not invent links not supported by the document.\n\n"
        f"Document ({upload.display_name}):\n{combined_text[:MAX_EXTRACTION_CHARS]}"
    )

    invoke = extractor_fn or _invoke_gemini_json_extractor
    raw_json, usage = invoke(
        settings=settings,
        model_id=model_id,
        prompt_text=prompt,
        image_bytes=upload.content if is_image else None,
        image_mime=upload.canonical_mime if is_image else None,
    )

    dropped_invalid = 0
    candidate_entities: list[EntityNode] = []
    name_to_id: dict[str, str] = {}

    for item in raw_json.get("entities") or []:
        if not isinstance(item, dict):
            dropped_invalid += 1
            continue
        cname = str(item.get("canonical_name") or "").strip()
        etype = str(item.get("entity_type") or "").strip()
        if not cname or etype not in ontology.allowed_node_types:
            dropped_invalid += 1
            continue
        raw_risk = str(item.get("risk_tier") or "MEDIUM").upper()
        risk_tier = RiskTier(raw_risk) if raw_risk in RiskTier._value2member_map_ else RiskTier.MEDIUM
        cid = str(item.get("source_chunk_id") or default_chunk_id)
        if cid not in chunk_ids_valid:
            cid = default_chunk_id
        eid = _make_id("UPL-ENT", f"{upload.pack_id}|{etype}|{cname.casefold()}", cname)
        try:
            node = EntityNode(
                pack_id=upload.pack_id,
                entity_id=eid,
                canonical_name=cname[:240],
                entity_type=etype,
                jurisdiction=str(item.get("jurisdiction") or "UNKNOWN")[:64],
                risk_tier=risk_tier,
                exposure_usd=max(0.0, float(item.get("exposure_usd") or 0.0)),
                single_source=bool(item.get("single_source", False)),
                data_origin=DataOrigin.CUSTOMER_UPLOAD,
                source_license=SourceLicense.CUSTOMER_PRIVATE,
                source_chunk_ids=(cid,),
                attributes={"uploaded_doc": upload.display_name, "upload_sha256": upload.sha256[:16]},
            )
        except (ValidationError, ValueError, TypeError):
            dropped_invalid += 1
            continue
        candidate_entities.append(node)
        name_to_id[cname.casefold()] = node.entity_id

    # Include existing pack names in lookup so edges can reference existing pack vertices directly
    for ex in existing_entities:
        name_to_id.setdefault(ex.canonical_name.casefold(), ex.entity_id)

    candidate_edges: list[GraphEdge] = []
    for item in raw_json.get("edges") or []:
        if not isinstance(item, dict):
            dropped_invalid += 1
            continue
        src_name = str(item.get("source_name") or "").strip()
        dst_name = str(item.get("target_name") or "").strip()
        rtype = str(item.get("relation_type") or "").strip()
        src_id = name_to_id.get(src_name.casefold())
        dst_id = name_to_id.get(dst_name.casefold())
        if not src_id or not dst_id or src_id == dst_id or rtype not in ontology.allowed_edge_types:
            dropped_invalid += 1
            continue
        cid = str(item.get("source_chunk_id") or default_chunk_id)
        if cid not in chunk_ids_valid:
            cid = default_chunk_id
        excerpt = str(item.get("evidence_excerpt") or "").strip()
        if len(excerpt) < 10:
            excerpt = f"Extracted {rtype} link between {src_name} and {dst_name} from {upload.display_name}."
        edge_id = _make_id("UPL-EDG", f"{upload.pack_id}|{src_id}|{rtype}|{dst_id}", f"{rtype}-{src_name[:12]}")
        try:
            edge = GraphEdge(
                pack_id=upload.pack_id,
                edge_id=edge_id,
                source_entity_id=src_id,
                target_entity_id=dst_id,
                relation_type=rtype,
                weight=1.0,
                exposure_usd=max(0.0, float(item.get("exposure_usd") or 0.0)),
                confidence=min(1.0, max(0.1, float(item.get("confidence") or 0.8))),
                evidence_excerpt=excerpt[:1500],
                source_chunk_id=cid,
                source_doc_name=upload.display_name,
                data_origin=DataOrigin.CUSTOMER_UPLOAD,
                source_license=SourceLicense.CUSTOMER_PRIVATE,
            )
        except (ValidationError, ValueError, TypeError):
            dropped_invalid += 1
            continue
        candidate_edges.append(edge)

    # Canonical O(N) entity resolution combined with existing pack entities
    combined_resolution = resolve_entities([*existing_entities, *candidate_entities])
    remapped_edges = remap_edges(candidate_edges, combined_resolution.alias_map)

    existing_ids = {e.entity_id for e in existing_entities}
    touched_ids = {combined_resolution.alias_map.get(e.entity_id, e.entity_id) for e in candidate_entities}
    # Surface newly created or enriched entities (excluding untouched pre-existing nodes)
    result_entities = tuple(
        e for e in combined_resolution.canonical_entities if e.entity_id in touched_ids or e.entity_id not in existing_ids
    )
    merged_count = max(
        0,
        len(candidate_entities) - sum(1 for e in result_entities if e.entity_id not in existing_ids),
    )

    cost_usd = compute_llm_cost_usd(model_id, usage)
    return ExtractionOutcome(
        chunks=chunks,
        entities=result_entities,
        edges=remapped_edges,
        merged_entities_count=merged_count,
        dropped_invalid_count=dropped_invalid,
        model_armor_checked=ma_verdict.checked,
        usage=usage,
        extraction_cost_usd=cost_usd,
    )

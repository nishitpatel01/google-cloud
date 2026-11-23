import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force";
import { Maximize2, Minimize2, RotateCcw, Search, ZoomIn, ZoomOut } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type PointerEvent } from "react";
import type { EntityNode, GraphEdge, OntologySpec } from "../types/graphrag";

interface LayoutNode extends SimulationNodeDatum {
  id: string;
  node: EntityNode;
  x: number;
  y: number;
}

interface LayoutLink extends SimulationLinkDatum<LayoutNode> {
  edge: GraphEdge;
  source: LayoutNode;
  target: LayoutNode;
}

interface GraphCanvasProps {
  nodes: EntityNode[];
  edges: GraphEdge[];
  ontology: OntologySpec;
  highlightedNodeIds?: Set<string>;
  highlightedEdgeIds?: Set<string>;
  selectedNodeId: string | null;
  selectedEdgeId: string | null;
  onSelectNode: (node: EntityNode) => void;
  onSelectEdge: (edge: GraphEdge) => void;
  isExpanded?: boolean;
  onToggleExpand?: () => void;
}

const VIEW_W = 1160;
const VIEW_H = 740;
const MIN_ZOOM = 0.45;
const MAX_ZOOM = 3.2;

// Harmonize default high-saturation ontology hexes into earthy Bohemian mineral tones
// while passing any custom user-defined ontology hexes through unchanged.
const BOHEMIAN_NODE_TONES: Record<string, string> = {
  "#2563EB": "#4d6b7d", // Slate Indigo / Denim
  "#0891B2": "#4d7d74", // Eucalyptus Teal
  "#7C3AED": "#885f74", // Dusty Fig / Plum Clay
  "#D97706": "#c08036", // Warm Spiced Ochre
  "#DC2626": "#b24a38", // Sun-Baked Terracotta Rust
};

function resolveNodeTone(rawColor: string): string {
  return BOHEMIAN_NODE_TONES[rawColor.toUpperCase()] ?? rawColor;
}

function clampZoom(z: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Number(z.toFixed(2))));
}

function computeForceLayout(
  nodes: EntityNode[],
  edges: GraphEdge[]
): { layoutNodes: LayoutNode[]; layoutLinks: LayoutLink[] } {
  if (nodes.length === 0) return { layoutNodes: [], layoutLinks: [] };

  // Deterministic circular initial seeding so layout stays calm between renders
  const simNodes: LayoutNode[] = nodes.map((node, idx) => {
    const angle = (2 * Math.PI * idx) / Math.max(1, nodes.length);
    const radius = Math.min(VIEW_W, VIEW_H) * 0.36;
    return {
      id: node.entity_id,
      node,
      x: VIEW_W / 2 + Math.cos(angle) * radius,
      y: VIEW_H / 2 + Math.sin(angle) * radius,
    };
  });

  const nodeMap = new Map(simNodes.map((n) => [n.id, n]));
  const simLinks: Array<{ source: string; target: string; edge: GraphEdge }> = [];
  for (const edge of edges) {
    if (nodeMap.has(edge.source_entity_id) && nodeMap.has(edge.target_entity_id)) {
      simLinks.push({ source: edge.source_entity_id, target: edge.target_entity_id, edge });
    }
  }

  const simulation = forceSimulation<LayoutNode>(simNodes)
    .force(
      "link",
      forceLink<
        LayoutNode,
        { source: string | LayoutNode; target: string | LayoutNode; edge: GraphEdge }
      >(simLinks)
        .id((d) => d.id)
        .distance(126)
        .strength(0.48)
    )
    .force("charge", forceManyBody().strength(-420))
    .force("center", forceCenter(VIEW_W / 2, VIEW_H / 2))
    .force("collide", forceCollide<LayoutNode>().radius(48))
    .stop();

  for (let i = 0; i < 190; i += 1) {
    simulation.tick();
  }

  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const n of simNodes) {
    if (n.x < minX) minX = n.x;
    if (n.x > maxX) maxX = n.x;
    if (n.y < minY) minY = n.y;
    if (n.y > maxY) maxY = n.y;
  }
  const pad = 74;
  const spanX = Math.max(1, maxX - minX);
  const spanY = Math.max(1, maxY - minY);
  const scale = Math.min((VIEW_W - pad * 2) / spanX, (VIEW_H - pad * 2) / spanY, 1.4);
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;

  for (const n of simNodes) {
    n.x = VIEW_W / 2 + (n.x - cx) * scale;
    n.y = VIEW_H / 2 + (n.y - cy) * scale;
  }

  const resolvedLinks: LayoutLink[] = [];
  for (const l of simLinks) {
    const src = typeof l.source === "string" ? nodeMap.get(l.source) : (l.source as LayoutNode);
    const dst = typeof l.target === "string" ? nodeMap.get(l.target) : (l.target as LayoutNode);
    if (src && dst) {
      resolvedLinks.push({ source: src, target: dst, edge: l.edge });
    }
  }
  return { layoutNodes: simNodes, layoutLinks: resolvedLinks };
}

function renderShape(shape: string, r: number, fill: string, stroke: string, strokeWidth: number) {
  if (shape === "diamond") {
    const pts = `0,${-r * 1.2} ${r * 1.2},0 0,${r * 1.2} ${-r * 1.2},0`;
    return <polygon points={pts} fill={fill} stroke={stroke} strokeWidth={strokeWidth} />;
  }
  if (shape === "square") {
    return (
      <rect
        x={-r}
        y={-r}
        width={r * 2}
        height={r * 2}
        rx={4}
        fill={fill}
        stroke={stroke}
        strokeWidth={strokeWidth}
      />
    );
  }
  if (shape === "hexagon") {
    const pts = Array.from({ length: 6 }, (_, i) => {
      const a = (Math.PI / 3) * i - Math.PI / 6;
      return `${(Math.cos(a) * r * 1.18).toFixed(1)},${(Math.sin(a) * r * 1.18).toFixed(1)}`;
    }).join(" ");
    return <polygon points={pts} fill={fill} stroke={stroke} strokeWidth={strokeWidth} />;
  }
  return <circle r={r} fill={fill} stroke={stroke} strokeWidth={strokeWidth} />;
}

export function GraphCanvas({
  nodes,
  edges,
  ontology,
  highlightedNodeIds,
  highlightedEdgeIds,
  selectedNodeId,
  selectedEdgeId,
  onSelectNode,
  onSelectEdge,
  isExpanded = false,
  onToggleExpand,
}: GraphCanvasProps) {
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [isPanning, setIsPanning] = useState(false);
  const [searchTerm, setSearchTerm] = useState("");

  const svgRef = useRef<SVGSVGElement | null>(null);
  const dragRef = useRef<{
    active: boolean;
    pointerId: number;
    startClientX: number;
    startClientY: number;
    originPanX: number;
    originPanY: number;
    moved: boolean;
  }>({
    active: false,
    pointerId: -1,
    startClientX: 0,
    startClientY: 0,
    originPanX: 0,
    originPanY: 0,
    moved: false,
  });

  const typeMeta = useMemo(() => {
    const m = new Map<string, { color: string; shape: string; label: string }>();
    for (const nt of ontology.node_types) {
      m.set(nt.type, { color: resolveNodeTone(nt.color), shape: nt.shape, label: nt.label });
    }
    return m;
  }, [ontology]);

  const { layoutNodes, layoutLinks } = useMemo(
    () => computeForceLayout(nodes, edges),
    [nodes, edges]
  );

  const hasHighlight = Boolean(
    (highlightedNodeIds && highlightedNodeIds.size > 0) ||
      (highlightedEdgeIds && highlightedEdgeIds.size > 0)
  );
  const queryLower = searchTerm.trim().toLowerCase();

  const handlePointerDown = (e: PointerEvent<SVGSVGElement>) => {
    if (e.button !== 0) return;
    dragRef.current = {
      active: true,
      pointerId: e.pointerId,
      startClientX: e.clientX,
      startClientY: e.clientY,
      originPanX: pan.x,
      originPanY: pan.y,
      moved: false,
    };
  };

  const handlePointerMove = (e: PointerEvent<SVGSVGElement>) => {
    const drag = dragRef.current;
    if (!drag.active) return;
    const dxPx = e.clientX - drag.startClientX;
    const dyPx = e.clientY - drag.startClientY;
    if (!drag.moved && Math.hypot(dxPx, dyPx) < 4) return;

    if (!drag.moved) {
      drag.moved = true;
      setIsPanning(true);
      e.currentTarget.setPointerCapture?.(e.pointerId);
    }

    const rect = svgRef.current?.getBoundingClientRect();
    const scaleX = rect && rect.width > 0 ? VIEW_W / rect.width : 1;
    const scaleY = rect && rect.height > 0 ? VIEW_H / rect.height : 1;
    setPan({
      x: drag.originPanX + dxPx * scaleX,
      y: drag.originPanY + dyPx * scaleY,
    });
  };

  const endPan = (e: PointerEvent<SVGSVGElement>) => {
    const drag = dragRef.current;
    if (!drag.active) return;
    if (drag.moved) {
      e.currentTarget.releasePointerCapture?.(drag.pointerId);
    }
    drag.active = false;
    setIsPanning(false);
  };

  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const onWheelNative = (e: globalThis.WheelEvent) => {
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.12 : 0.89;
      setZoom((z) => clampZoom(z * factor));
    };
    svg.addEventListener("wheel", onWheelNative, { passive: false });
    return () => {
      svg.removeEventListener("wheel", onWheelNative);
    };
  }, []);

  const handleResetView = () => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
    setSearchTerm("");
  };

  return (
    <div className="panel">
      <div className="panel-header">
        <div className="row-wrap">
          <h2 className="panel-title">Connected Risk Network</h2>
          <span className="badge">
            {nodes.length} entities · {edges.length} connections
          </span>
          {hasHighlight && (
            <span className="badge badge-verified">
              {highlightedNodeIds?.size ?? 0} in active risk chain
            </span>
          )}
        </div>

        <div className="graph-toolbar">
          <div className="search-wrap">
            <Search size={13} aria-hidden="true" className="search-icon" />
            <input
              type="search"
              aria-label="Filter network entities"
              placeholder="Find company, facility, or clause..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="text-input search-input"
            />
          </div>
          <button
            type="button"
            className="btn btn-sm"
            aria-label="Zoom out graph"
            onClick={() => setZoom((z) => clampZoom(z - 0.25))}
          >
            <ZoomOut size={14} aria-hidden="true" />
          </button>
          <span className="badge mono" aria-label="Current zoom level">
            {Math.round(zoom * 100)}%
          </span>
          <button
            type="button"
            className="btn btn-sm"
            aria-label="Zoom in graph"
            onClick={() => setZoom((z) => clampZoom(z + 0.25))}
          >
            <ZoomIn size={14} aria-hidden="true" />
          </button>
          <button
            type="button"
            className="btn btn-sm"
            aria-label="Fit and reset graph view"
            title="Reset zoom, pan, and filter"
            onClick={handleResetView}
          >
            <RotateCcw size={13} aria-hidden="true" />
            Fit
          </button>
          {onToggleExpand && (
            <button
              type="button"
              className="btn btn-sm"
              aria-pressed={isExpanded}
              aria-label={isExpanded ? "Restore standard graph size" : "Expand graph full width"}
              title={isExpanded ? "Restore 3-column layout" : "Expand graph to full screen width"}
              onClick={onToggleExpand}
            >
              {isExpanded ? (
                <>
                  <Minimize2 size={13} aria-hidden="true" />
                  Standard
                </>
              ) : (
                <>
                  <Maximize2 size={13} aria-hidden="true" />
                  Expand
                </>
              )}
            </button>
          )}
        </div>
      </div>

      <div className="graph-legend" aria-label="Entity categories">
        <div className="row-wrap">
          {ontology.node_types.map((nt) => (
            <span key={nt.type} className="legend-item">
              <svg width={10} height={10} aria-hidden="true">
                <rect width={10} height={10} rx={3} fill={resolveNodeTone(nt.color)} />
              </svg>
              {nt.label}
            </span>
          ))}
        </div>
        <span className="graph-hint">Drag to pan · Scroll to zoom · Click to inspect</span>
      </div>

      <div className={`graph-canvas-wrap${isExpanded ? " expanded" : ""}`}>
        <svg
          ref={svgRef}
          className={`graph-svg${isPanning ? " panning" : ""}`}
          viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
          role="img"
          aria-label={`Interactive risk network with ${nodes.length} entities and ${edges.length} relationships`}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={endPan}
          onPointerCancel={endPan}
        >
          <defs>
            <marker
              id="arrow-default"
              viewBox="0 0 10 10"
              refX="24"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 1.5 L 10 5 L 0 8.5 z" fill="var(--graph-edge)" />
            </marker>
            <marker
              id="arrow-active"
              viewBox="0 0 10 10"
              refX="24"
              refY="5"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
            >
              <path d="M 0 1.5 L 10 5 L 0 8.5 z" fill="var(--graph-edge-active)" />
            </marker>
            <marker
              id="arrow-selected"
              viewBox="0 0 10 10"
              refX="24"
              refY="5"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
            >
              <path d="M 0 1.5 L 10 5 L 0 8.5 z" fill="var(--accent-primary)" />
            </marker>
          </defs>

          <g
            transform={`translate(${VIEW_W / 2 + pan.x}, ${VIEW_H / 2 + pan.y}) scale(${zoom}) translate(${-VIEW_W / 2}, ${-VIEW_H / 2})`}
          >
            {layoutLinks.map((link) => {
              const eid = link.edge.edge_id;
              const isHighlighted = highlightedEdgeIds?.has(eid) ?? false;
              const isSelected = selectedEdgeId === eid;
              const dimmed = hasHighlight && !isHighlighted && !isSelected;

              return (
                <g
                  key={eid}
                  role="button"
                  tabIndex={0}
                  aria-label={`Connection ${link.edge.relation_type} from ${link.source.node.canonical_name} to ${link.target.node.canonical_name}`}
                  onClick={() => {
                    if (!dragRef.current.moved) onSelectEdge(link.edge);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      onSelectEdge(link.edge);
                    }
                  }}
                  className="clickable-svg"
                >
                  <line
                    x1={link.source.x}
                    y1={link.source.y}
                    x2={link.target.x}
                    y2={link.target.y}
                    stroke="transparent"
                    strokeWidth={14}
                  />
                  <line
                    x1={link.source.x}
                    y1={link.source.y}
                    x2={link.target.x}
                    y2={link.target.y}
                    stroke={
                      isSelected
                        ? "var(--accent-primary)"
                        : isHighlighted
                          ? "var(--graph-edge-active)"
                          : "var(--graph-edge)"
                    }
                    strokeWidth={isSelected || isHighlighted ? 2.6 : 1.45}
                    strokeOpacity={dimmed ? 0.22 : 0.9}
                    strokeDasharray={
                      link.edge.data_origin === "MODELED_BRIDGE" ? "5 3" : undefined
                    }
                    markerEnd={
                      isSelected
                        ? "url(#arrow-selected)"
                        : isHighlighted
                          ? "url(#arrow-active)"
                          : "url(#arrow-default)"
                    }
                  />
                </g>
              );
            })}

            {layoutNodes.map((ln) => {
              const n = ln.node;
              const meta = typeMeta.get(n.entity_type) ?? {
                color: "var(--ink-muted)",
                shape: "circle",
                label: n.entity_type,
              };
              const isHighlighted = highlightedNodeIds?.has(n.entity_id) ?? false;
              const isSelected = selectedNodeId === n.entity_id;
              const matchesSearch =
                queryLower.length > 0 &&
                (n.canonical_name.toLowerCase().includes(queryLower) ||
                  n.entity_id.toLowerCase().includes(queryLower));
              const dimmed =
                (hasHighlight && !isHighlighted && !isSelected) ||
                (queryLower.length > 0 && !matchesSearch && !isSelected);
              const radius = isSelected ? 16.5 : isHighlighted || matchesSearch ? 14 : 11.5;
              const maxChars = zoom >= 1.35 || isExpanded ? 32 : 24;
              const shortLabel =
                n.canonical_name.length > maxChars
                  ? `${n.canonical_name.slice(0, maxChars - 1)}…`
                  : n.canonical_name;

              return (
                <g
                  key={n.entity_id}
                  transform={`translate(${ln.x}, ${ln.y})`}
                  role="button"
                  tabIndex={0}
                  aria-label={`${n.canonical_name} (${meta.label}, risk ${n.risk_tier})`}
                  onClick={() => {
                    if (!dragRef.current.moved) onSelectNode(n);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      onSelectNode(n);
                    }
                  }}
                  className="clickable-svg"
                  opacity={dimmed ? 0.26 : 1}
                >
                  {renderShape(
                    meta.shape,
                    radius,
                    meta.color,
                    isSelected
                      ? "var(--graph-node-selected)"
                      : isHighlighted
                        ? "var(--graph-edge-active)"
                        : "var(--graph-node-stroke)",
                    isSelected ? 3 : isHighlighted ? 2.4 : 1.8
                  )}
                  {(isHighlighted ||
                    isSelected ||
                    matchesSearch ||
                    zoom >= 1.15 ||
                    isExpanded ||
                    nodes.length <= 45) && (
                    <text
                      className="graph-node-label"
                      textAnchor="middle"
                      y={radius + 14}
                    >
                      {shortLabel}
                    </text>
                  )}
                </g>
              );
            })}
          </g>
        </svg>
      </div>
    </div>
  );
}

import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force";
import { RotateCcw, Search, ZoomIn, ZoomOut } from "lucide-react";
import { useMemo, useState } from "react";
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
}

const VIEW_W = 860;
const VIEW_H = 510;

function computeForceLayout(
  nodes: EntityNode[],
  edges: GraphEdge[]
): { layoutNodes: LayoutNode[]; layoutLinks: LayoutLink[] } {
  if (nodes.length === 0) return { layoutNodes: [], layoutLinks: [] };

  // Deterministic circular initial seeding so layout stays calm between renders
  const simNodes: LayoutNode[] = nodes.map((node, idx) => {
    const angle = (2 * Math.PI * idx) / Math.max(1, nodes.length);
    const radius = Math.min(VIEW_W, VIEW_H) * 0.32;
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
        .distance(88)
        .strength(0.55)
    )
    .force("charge", forceManyBody().strength(-260))
    .force("center", forceCenter(VIEW_W / 2, VIEW_H / 2))
    .force("collide", forceCollide<LayoutNode>().radius(34))
    .stop();

  for (let i = 0; i < 160; i += 1) {
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
  const pad = 64;
  const spanX = Math.max(1, maxX - minX);
  const spanY = Math.max(1, maxY - minY);
  const scale = Math.min((VIEW_W - pad * 2) / spanX, (VIEW_H - pad * 2) / spanY, 1.35);
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
}: GraphCanvasProps) {
  const [zoom, setZoom] = useState(1);
  const [searchTerm, setSearchTerm] = useState("");

  const typeMeta = useMemo(() => {
    const m = new Map<string, { color: string; shape: string; label: string }>();
    for (const nt of ontology.node_types) {
      m.set(nt.type, { color: nt.color, shape: nt.shape, label: nt.label });
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
            aria-label="Zoom in graph"
            onClick={() => setZoom((z) => Math.min(2.2, Number((z + 0.2).toFixed(2))))}
          >
            <ZoomIn size={14} aria-hidden="true" />
          </button>
          <button
            type="button"
            className="btn btn-sm"
            aria-label="Zoom out graph"
            onClick={() => setZoom((z) => Math.max(0.6, Number((z - 0.2).toFixed(2))))}
          >
            <ZoomOut size={14} aria-hidden="true" />
          </button>
          <button
            type="button"
            className="btn btn-sm"
            aria-label="Reset graph zoom"
            onClick={() => {
              setZoom(1);
              setSearchTerm("");
            }}
          >
            <RotateCcw size={13} aria-hidden="true" />
          </button>
        </div>
      </div>

      <div className="graph-legend" aria-label="Entity categories">
        {ontology.node_types.map((nt) => (
          <span key={nt.type} className="legend-item">
            <svg width={10} height={10} aria-hidden="true">
              <rect width={10} height={10} rx={3} fill={nt.color} />
            </svg>
            {nt.label}
          </span>
        ))}
      </div>

      <div className="graph-canvas-wrap">
        <svg
          className="graph-svg"
          viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
          role="img"
          aria-label={`Interactive risk network with ${nodes.length} entities and ${edges.length} relationships`}
        >
          <defs>
            <marker
              id="arrow-default"
              viewBox="0 0 10 10"
              refX="22"
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
              refX="22"
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
              refX="22"
              refY="5"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
            >
              <path d="M 0 1.5 L 10 5 L 0 8.5 z" fill="var(--accent-primary)" />
            </marker>
          </defs>

          <g
            transform={`translate(${VIEW_W / 2}, ${VIEW_H / 2}) scale(${zoom}) translate(${-VIEW_W / 2}, ${-VIEW_H / 2})`}
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
                  onClick={() => onSelectEdge(link.edge)}
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
                    strokeWidth={12}
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
                    strokeWidth={isSelected || isHighlighted ? 2.5 : 1.35}
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
              const radius = isSelected ? 15 : isHighlighted || matchesSearch ? 13 : 10.5;
              const shortLabel =
                n.canonical_name.length > 22
                  ? `${n.canonical_name.slice(0, 21)}…`
                  : n.canonical_name;

              return (
                <g
                  key={n.entity_id}
                  transform={`translate(${ln.x}, ${ln.y})`}
                  role="button"
                  tabIndex={0}
                  aria-label={`${n.canonical_name} (${meta.label}, risk ${n.risk_tier})`}
                  onClick={() => onSelectNode(n)}
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
                  {(isHighlighted || isSelected || matchesSearch || nodes.length <= 36) && (
                    <text
                      className="graph-node-label"
                      textAnchor="middle"
                      y={radius + 13}
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

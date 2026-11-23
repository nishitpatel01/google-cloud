import {
  Activity,
  BarChart3,
  GitCompare,
  Network,
  ShieldCheck,
  Upload,
} from "lucide-react";
import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import type { MetaResponse, PackSummary } from "../types/graphrag";

interface ShellProps {
  meta: MetaResponse | null;
  packs: PackSummary[];
  activePackId: string;
  onSelectPack: (packId: string) => void;
  mode: "replay" | "live";
  onChangeMode: (mode: "replay" | "live") => void;
  synthesisModel: string;
  onChangeModel: (modelId: string) => void;
  children: ReactNode;
}

export function Shell({
  meta,
  packs,
  activePackId,
  onSelectPack,
  mode,
  onChangeMode,
  synthesisModel,
  onChangeModel,
  children,
}: ShellProps) {
  const liveAllowed = Boolean(meta?.live_capable);
  const models = meta?.models ?? [];

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="topbar-inner">
          <div className="brand">
            <span className="brand-mark" aria-hidden="true">
              <Network size={18} />
            </span>
            <div>
              <div className="brand-title">Enterprise GraphRAG</div>
              <div className="brand-sub">
                BigQuery Property Graph (ISO GQL) · Google ADK 2.15
              </div>
            </div>
          </div>

          <nav className="nav-tabs" aria-label="Primary workspace navigation">
            <NavLink
              to="/"
              end
              className={({ isActive }) => `nav-tab${isActive ? " active" : ""}`}
            >
              <Activity size={15} aria-hidden="true" />
              Explorer
            </NavLink>
            <NavLink
              to="/compare"
              className={({ isActive }) => `nav-tab${isActive ? " active" : ""}`}
            >
              <GitCompare size={15} aria-hidden="true" />
              Compare Lab
            </NavLink>
            <NavLink
              to="/ingest"
              className={({ isActive }) => `nav-tab${isActive ? " active" : ""}`}
            >
              <Upload size={15} aria-hidden="true" />
              BYO Ingest
            </NavLink>
            <NavLink
              to="/benchmark"
              className={({ isActive }) => `nav-tab${isActive ? " active" : ""}`}
            >
              <BarChart3 size={15} aria-hidden="true" />
              Benchmark &amp; TCO
            </NavLink>
          </nav>

          <div className="header-controls">
            <label className="sr-only" htmlFor="pack-select">
              Industry Pack
            </label>
            <select
              id="pack-select"
              aria-label="Select industry pack"
              className="select-control"
              value={activePackId}
              onChange={(e) => onSelectPack(e.target.value)}
            >
              {packs.map((p) => (
                <option key={p.pack_id} value={p.pack_id}>
                  {p.title}
                </option>
              ))}
            </select>

            <label className="sr-only" htmlFor="model-select">
              Synthesis Model
            </label>
            <select
              id="model-select"
              aria-label="Select synthesis model"
              className="select-control"
              value={synthesisModel}
              onChange={(e) => onChangeModel(e.target.value)}
            >
              {models.map((m) => (
                <option key={m.model_id} value={m.model_id}>
                  {m.display_name}
                </option>
              ))}
            </select>

            <div className="mode-switch" role="group" aria-label="Execution mode">
              <button
                type="button"
                className="mode-btn"
                aria-pressed={mode === "live"}
                disabled={!liveAllowed}
                title={
                  liveAllowed
                    ? "Execute live against BigQuery Property Graph & Vertex AI"
                    : "Configure GCP_PROJECT_ID to enable Live mode"
                }
                onClick={() => onChangeMode("live")}
              >
                Live
              </button>
              <button
                type="button"
                className="mode-btn"
                aria-pressed={mode === "replay"}
                onClick={() => onChangeMode("replay")}
              >
                Replay
              </button>
            </div>

            <span
              className={`badge ${
                meta?.model_armor_configured ? "badge-verified" : ""
              }`}
              title={
                meta?.model_armor_configured
                  ? "Google Cloud Model Armor prompt screening active"
                  : "Set MODEL_ARMOR_TEMPLATE_ID to enable Model Armor screening"
              }
            >
              <ShieldCheck size={13} aria-hidden="true" />
              {meta?.model_armor_configured ? "Model Armor On" : "Model Armor Optional"}
            </span>
          </div>
        </div>
      </header>

      <main className="main-content">{children}</main>
    </div>
  );
}

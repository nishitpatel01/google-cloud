import {
  Activity,
  BarChart3,
  GitCompare,
  History,
  Monitor,
  Moon,
  Network,
  ShieldCheck,
  Sun,
  Upload,
} from "lucide-react";
import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import type { MetaResponse, PackSummary, ThemePreference } from "../types/graphrag";

export interface ActiveJobsMap {
  explorer: boolean;
  compare: boolean;
  ingest: boolean;
}

interface ShellProps {
  meta: MetaResponse | null;
  packs: PackSummary[];
  activePackId: string;
  onSelectPack: (packId: string) => void;
  mode: "replay" | "live";
  onChangeMode: (mode: "replay" | "live") => void;
  synthesisModel: string;
  onChangeModel: (modelId: string) => void;
  theme: ThemePreference;
  onChangeTheme: (theme: ThemePreference) => void;
  activeJobs: ActiveJobsMap;
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
  theme,
  onChangeTheme,
  activeJobs,
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
              <div className="brand-title">Risk &amp; Contagion Intelligence</div>
              <div className="brand-sub">
                Uncover hidden multi-tier risk across suppliers, sanctions, and contracts
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
              Risk Explorer
              {activeJobs.explorer && (
                <span
                  className="tab-running-dot"
                  title="Investigation running in background"
                  aria-label="Running"
                />
              )}
            </NavLink>
            <NavLink
              to="/compare"
              className={({ isActive }) => `nav-tab${isActive ? " active" : ""}`}
            >
              <GitCompare size={15} aria-hidden="true" />
              AI Comparison
              {activeJobs.compare && (
                <span
                  className="tab-running-dot"
                  title="Comparison running in background"
                  aria-label="Running"
                />
              )}
            </NavLink>
            <NavLink
              to="/ingest"
              className={({ isActive }) => `nav-tab${isActive ? " active" : ""}`}
            >
              <Upload size={15} aria-hidden="true" />
              Upload Documents
              {activeJobs.ingest && (
                <span
                  className="tab-running-dot"
                  title="Document extraction running in background"
                  aria-label="Running"
                />
              )}
            </NavLink>
            <NavLink
              to="/benchmark"
              className={({ isActive }) => `nav-tab${isActive ? " active" : ""}`}
            >
              <BarChart3 size={15} aria-hidden="true" />
              ROI &amp; Benchmarks
            </NavLink>
            <NavLink
              to="/releases"
              className={({ isActive }) => `nav-tab${isActive ? " active" : ""}`}
            >
              <History size={15} aria-hidden="true" />
              Releases
            </NavLink>
          </nav>

          <div className="header-controls">
            <label className="sr-only" htmlFor="pack-select">
              Industry Scenario
            </label>
            <select
              id="pack-select"
              aria-label="Select industry scenario"
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
              AI Reasoning Model
            </label>
            <select
              id="model-select"
              aria-label="Select AI reasoning model"
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

            <div className="segmented-group" role="group" aria-label="Execution mode">
              <button
                type="button"
                className="segmented-btn"
                aria-pressed={mode === "live"}
                disabled={!liveAllowed}
                title={
                  liveAllowed
                    ? "Run live queries against BigQuery Property Graph and Vertex AI"
                    : "Configure GCP_PROJECT_ID to enable Live Cloud mode"
                }
                onClick={() => onChangeMode("live")}
              >
                Live Cloud
              </button>
              <button
                type="button"
                className="segmented-btn"
                aria-pressed={mode === "replay"}
                title="Replay verified cached benchmarks without cloud charges"
                onClick={() => onChangeMode("replay")}
              >
                Replay
              </button>
            </div>

            <div className="segmented-group" role="group" aria-label="Color theme">
              <button
                type="button"
                className="segmented-btn"
                aria-pressed={theme === "system"}
                title="Match operating system theme"
                onClick={() => onChangeTheme("system")}
              >
                <Monitor size={13} aria-hidden="true" />
                System
              </button>
              <button
                type="button"
                className="segmented-btn"
                aria-pressed={theme === "light"}
                title="Switch to soft light theme"
                onClick={() => onChangeTheme("light")}
              >
                <Sun size={13} aria-hidden="true" />
                Light
              </button>
              <button
                type="button"
                className="segmented-btn"
                aria-pressed={theme === "dark"}
                title="Switch to low-glare dark theme"
                onClick={() => onChangeTheme("dark")}
              >
                <Moon size={13} aria-hidden="true" />
                Dark
              </button>
            </div>

            <span
              className={`badge ${
                meta?.model_armor_configured ? "badge-verified" : ""
              }`}
              title={
                meta?.model_armor_configured
                  ? "Google Cloud Model Armor prompt & safety screening active"
                  : "Set MODEL_ARMOR_TEMPLATE_ID to enable Google Cloud Model Armor screening"
              }
            >
              <ShieldCheck size={13} aria-hidden="true" />
              {meta?.model_armor_configured
                ? "Safety Guardrails Active"
                : "Guardrails Optional"}
            </span>
          </div>
        </div>
      </header>

      <main className="main-content">{children}</main>
    </div>
  );
}

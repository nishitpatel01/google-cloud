import { useCallback, useEffect, useState } from "react";
import { BrowserRouter, useLocation, useNavigate } from "react-router-dom";
import { fetchMeta, fetchPacks } from "./api/client";
import { Shell, type ActiveJobsMap } from "./components/Shell";
import { BenchmarkPage } from "./pages/BenchmarkPage";
import { ComparePage } from "./pages/ComparePage";
import { ExplorerPage } from "./pages/ExplorerPage";
import { IngestPage } from "./pages/IngestPage";
import { ReleasesPage } from "./pages/ReleasesPage";
import type { MetaResponse, PackSummary, ThemePreference } from "./types/graphrag";

type TabKey = "explorer" | "compare" | "ingest" | "benchmark" | "releases";

function pathnameToTab(pathname: string): TabKey | null {
  if (pathname === "/" || pathname === "") return "explorer";
  if (pathname === "/compare") return "compare";
  if (pathname === "/ingest") return "ingest";
  if (pathname === "/benchmark") return "benchmark";
  if (pathname === "/releases") return "releases";
  return null;
}

interface WorkspaceRouterProps {
  meta: MetaResponse | null;
  packs: PackSummary[];
  activePack: PackSummary | null;
  onSelectPack: (packId: string) => void;
  mode: "replay" | "live";
  onChangeMode: (mode: "replay" | "live") => void;
  synthesisModel: string;
  onChangeModel: (modelId: string) => void;
  theme: ThemePreference;
  onChangeTheme: (theme: ThemePreference) => void;
  bootError: string | null;
}

function WorkspaceRouter({
  meta,
  packs,
  activePack,
  onSelectPack,
  mode,
  onChangeMode,
  synthesisModel,
  onChangeModel,
  theme,
  onChangeTheme,
  bootError,
}: WorkspaceRouterProps) {
  const location = useLocation();
  const navigate = useNavigate();
  const [activeJobs, setActiveJobs] = useState<ActiveJobsMap>({
    explorer: false,
    compare: false,
    ingest: false,
  });

  const resolvedTab = pathnameToTab(location.pathname);

  useEffect(() => {
    if (resolvedTab === null) {
      navigate("/", { replace: true });
    }
  }, [resolvedTab, navigate]);

  const activeTab: TabKey = resolvedTab ?? "explorer";

  const handleExplorerRunning = useCallback((running: boolean) => {
    setActiveJobs((prev) => (prev.explorer === running ? prev : { ...prev, explorer: running }));
  }, []);

  const handleCompareRunning = useCallback((running: boolean) => {
    setActiveJobs((prev) => (prev.compare === running ? prev : { ...prev, compare: running }));
  }, []);

  const handleIngestRunning = useCallback((running: boolean) => {
    setActiveJobs((prev) => (prev.ingest === running ? prev : { ...prev, ingest: running }));
  }, []);

  return (
    <Shell
      meta={meta}
      packs={packs}
      activePackId={activePack?.pack_id ?? ""}
      onSelectPack={onSelectPack}
      mode={mode}
      onChangeMode={onChangeMode}
      synthesisModel={synthesisModel}
      onChangeModel={onChangeModel}
      theme={theme}
      onChangeTheme={onChangeTheme}
      activeJobs={activeJobs}
    >
      {bootError ? (
        <div role="alert" className="error-banner">
          Unable to connect to the Risk &amp; Contagion Intelligence API: {bootError}
        </div>
      ) : !activePack ? (
        <div className="panel">
          <div className="panel-body">Loading verified industry scenarios…</div>
        </div>
      ) : (
        <>
          {/* Keep all 5 workspaces mounted so switching tabs NEVER drops active SSE streams, prompts, or results */}
          <div
            className={activeTab === "explorer" ? "" : "tab-pane-hidden"}
            aria-hidden={activeTab !== "explorer"}
          >
            <ExplorerPage
              pack={activePack}
              mode={mode}
              synthesisModel={synthesisModel}
              onRunningChange={handleExplorerRunning}
            />
          </div>

          <div
            className={activeTab === "compare" ? "" : "tab-pane-hidden"}
            aria-hidden={activeTab !== "compare"}
          >
            <ComparePage
              pack={activePack}
              mode={mode}
              synthesisModel={synthesisModel}
              onRunningChange={handleCompareRunning}
            />
          </div>

          <div
            className={activeTab === "ingest" ? "" : "tab-pane-hidden"}
            aria-hidden={activeTab !== "ingest"}
          >
            <IngestPage
              pack={activePack}
              mode={mode}
              synthesisModel={synthesisModel}
              onRunningChange={handleIngestRunning}
            />
          </div>

          <div
            className={activeTab === "benchmark" ? "" : "tab-pane-hidden"}
            aria-hidden={activeTab !== "benchmark"}
          >
            <BenchmarkPage pack={activePack} synthesisModel={synthesisModel} />
          </div>

          <div
            className={activeTab === "releases" ? "" : "tab-pane-hidden"}
            aria-hidden={activeTab !== "releases"}
          >
            <ReleasesPage />
          </div>
        </>
      )}
    </Shell>
  );
}

export function App() {
  const [meta, setMeta] = useState<MetaResponse | null>(null);
  const [packs, setPacks] = useState<PackSummary[]>([]);
  const [activePackId, setActivePackId] = useState<string>("");
  const [mode, setMode] = useState<"replay" | "live">("replay");
  const [synthesisModel, setSynthesisModel] = useState<string>("gemini-2.5-flash");
  const [theme, setTheme] = useState<ThemePreference>("system");
  const [bootError, setBootError] = useState<string | null>(null);

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const applyTheme = () => {
      const resolved = theme === "system" ? (media.matches ? "dark" : "light") : theme;
      document.documentElement.dataset.theme = resolved;
    };
    applyTheme();
    media.addEventListener("change", applyTheme);
    return () => {
      media.removeEventListener("change", applyTheme);
    };
  }, [theme]);

  useEffect(() => {
    let cancelled = false;
    Promise.all([fetchMeta(), fetchPacks()])
      .then(([m, pList]) => {
        if (cancelled) return;
        setMeta(m);
        setPacks(pList);
        setSynthesisModel(m.default_synthesis_model || "gemini-2.5-flash");
        setMode(m.live_capable && m.demo_mode !== "replay" ? "live" : "replay");
        const preferred =
          pList.find((p) => p.pack_id === "supply_chain_subtier_risk") ?? pList[0];
        if (preferred) setActivePackId(preferred.pack_id);
      })
      .catch((err: Error) => {
        if (!cancelled) setBootError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const activePack = packs.find((p) => p.pack_id === activePackId) ?? packs[0] ?? null;

  return (
    <BrowserRouter>
      <WorkspaceRouter
        meta={meta}
        packs={packs}
        activePack={activePack}
        onSelectPack={setActivePackId}
        mode={mode}
        onChangeMode={setMode}
        synthesisModel={synthesisModel}
        onChangeModel={setSynthesisModel}
        theme={theme}
        onChangeTheme={setTheme}
        bootError={bootError}
      />
    </BrowserRouter>
  );
}

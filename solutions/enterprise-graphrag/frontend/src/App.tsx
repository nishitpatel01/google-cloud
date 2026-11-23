import { useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { fetchMeta, fetchPacks } from "./api/client";
import { Shell } from "./components/Shell";
import { BenchmarkPage } from "./pages/BenchmarkPage";
import { ComparePage } from "./pages/ComparePage";
import { ExplorerPage } from "./pages/ExplorerPage";
import { IngestPage } from "./pages/IngestPage";
import type { MetaResponse, PackSummary } from "./types/graphrag";

export function App() {
  const [meta, setMeta] = useState<MetaResponse | null>(null);
  const [packs, setPacks] = useState<PackSummary[]>([]);
  const [activePackId, setActivePackId] = useState<string>("");
  const [mode, setMode] = useState<"replay" | "live">("replay");
  const [synthesisModel, setSynthesisModel] = useState<string>("gemini-2.5-flash");
  const [bootError, setBootError] = useState<string | null>(null);

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
      <Shell
        meta={meta}
        packs={packs}
        activePackId={activePack?.pack_id ?? ""}
        onSelectPack={setActivePackId}
        mode={mode}
        onChangeMode={setMode}
        synthesisModel={synthesisModel}
        onChangeModel={setSynthesisModel}
      >
        {bootError ? (
          <div role="alert" className="error-banner">
            Unable to connect to Enterprise GraphRAG backend API: {bootError}
          </div>
        ) : !activePack ? (
          <div className="panel">
            <div className="panel-body">Loading verified industry packs…</div>
          </div>
        ) : (
          <Routes>
            <Route
              path="/"
              element={
                <ExplorerPage
                  pack={activePack}
                  mode={mode}
                  synthesisModel={synthesisModel}
                />
              }
            />
            <Route
              path="/compare"
              element={
                <ComparePage
                  pack={activePack}
                  mode={mode}
                  synthesisModel={synthesisModel}
                />
              }
            />
            <Route
              path="/ingest"
              element={
                <IngestPage
                  pack={activePack}
                  mode={mode}
                  synthesisModel={synthesisModel}
                />
              }
            />
            <Route
              path="/benchmark"
              element={
                <BenchmarkPage pack={activePack} synthesisModel={synthesisModel} />
              }
            />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        )}
      </Shell>
    </BrowserRouter>
  );
}

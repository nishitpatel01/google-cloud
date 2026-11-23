import { CheckCircle2, GitPullRequest, History, Sparkles } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { fetchReleases } from "../api/client";
import type { ReleasesResponse } from "../types/graphrag";

export function ReleasesPage() {
  const [data, setData] = useState<ReleasesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedCategory, setSelectedCategory] = useState<string>("All");

  useEffect(() => {
    let cancelled = false;
    fetchReleases()
      .then((res) => {
        if (!cancelled) setData(res);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const categories = useMemo(() => {
    if (!data) return ["All"];
    const unique = new Set<string>();
    for (const r of data.releases) {
      for (const c of r.categories) unique.add(c);
    }
    return ["All", ...Array.from(unique)];
  }, [data]);

  const visibleReleases = useMemo(() => {
    if (!data) return [];
    if (selectedCategory === "All") return data.releases;
    return data.releases.filter((r) => r.categories.includes(selectedCategory));
  }, [data, selectedCategory]);

  return (
    <div className="stack-md">
      <div className="page-header">
        <div>
          <h1>Product Releases &amp; Delivery Log</h1>
          <p className="page-subtitle">
            Track every milestone, customer capability, security control, and live Google Cloud
            verification shipped for this solution.
          </p>
        </div>
        {data && (
          <div className="row-wrap">
            <span className="badge badge-verified">
              <Sparkles size={12} aria-hidden="true" />
              Current Version v{data.current_version}
            </span>
            <span className="badge">
              <History size={12} aria-hidden="true" />
              {data.releases.length} Releases Shipped
            </span>
            <span className="badge mono">Solution ID: {data.solution_id}</span>
          </div>
        )}
      </div>

      {error && (
        <div role="alert" className="error-banner">
          Unable to load release changelog: {error}
        </div>
      )}

      {!data && !error && (
        <div className="panel">
          <div className="panel-body">Loading release history…</div>
        </div>
      )}

      {data && (
        <>
          <section className="panel" aria-label="Filter releases by category">
            <div className="panel-body row-between">
              <div className="row-wrap">
                {categories.map((cat) => (
                  <button
                    key={cat}
                    type="button"
                    className={`btn btn-sm${selectedCategory === cat ? " btn-primary" : ""}`}
                    aria-pressed={selectedCategory === cat}
                    onClick={() => setSelectedCategory(cat)}
                  >
                    {cat}
                  </button>
                ))}
              </div>
              <span className="muted-copy">
                Showing {visibleReleases.length} of {data.releases.length} releases
              </span>
            </div>
          </section>

          <div className="release-list">
            {visibleReleases.map((rel) => {
              const isMerged = rel.status.toLowerCase() === "merged";
              return (
                <article key={rel.version} className="panel" aria-label={`Release ${rel.version}`}>
                  <div className="panel-header">
                    <div className="row-wrap">
                      <span className="badge badge-upload mono">v{rel.version}</span>
                      <span className="badge">{rel.phase}</span>
                      <span className={`badge ${isMerged ? "badge-verified" : "badge-modeled"}`}>
                        <CheckCircle2 size={12} aria-hidden="true" />
                        {rel.status}
                      </span>
                      <span className="badge mono">
                        <GitPullRequest size={12} aria-hidden="true" />
                        PR {rel.pr}
                      </span>
                    </div>
                    <span className="muted-copy mono">{rel.date}</span>
                  </div>

                  <div className="panel-body stack-sm">
                    <div className="node-title">{rel.title}</div>
                    <p className="page-subtitle">{rel.summary}</p>

                    <div className="row-wrap">
                      {rel.categories.map((cat) => (
                        <span key={cat} className="badge">
                          {cat}
                        </span>
                      ))}
                    </div>

                    <div>
                      <div className="field-label">What&apos;s Included</div>
                      <ul className="release-highlights">
                        {rel.highlights.map((item, idx) => (
                          <li key={idx}>{item}</li>
                        ))}
                      </ul>
                    </div>

                    <div className="row-wrap stage-meta">
                      <span className="badge badge-verified">
                        Backend Tests: {rel.verification.backend_tests} passed
                      </span>
                      {rel.verification.frontend_suites > 0 && (
                        <span className="badge badge-verified">
                          UI &amp; Security Suites: {rel.verification.frontend_suites} passed
                        </span>
                      )}
                      <span className="badge">
                        Live Verification: {rel.verification.gcp_live}
                      </span>
                    </div>
                  </div>
                </article>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
